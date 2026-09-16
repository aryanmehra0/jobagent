"""Strict Pydantic schemas for the candidate profile, search parameters, and pipeline artifacts.

Two guarantees are enforced here rather than in the individual pipeline stages, so
that no stage can bypass them:

1. **Normalization at the boundary.** Every string is unicode-normalized and
   whitespace-collapsed on the way in. Dates are canonicalized, URLs get a scheme
   or are dropped, and list fields are de-duplicated. Downstream code can therefore
   compare values with `==` and trust the result.
2. **Immutable fact locking.** Quantifiable achievements are recorded as
   `LockedFact` entries and sealed under a SHA-256 hash. The tailoring stage is
   barred from altering them, and `verify_integrity()` detects any mutation.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from job_agent.config.normalize import (
    clean_text,
    dedupe_preserving_order,
    is_present_token,
    normalize_date_string,
    normalize_phone,
    normalize_url,
    parse_partial_date,
    strip_bullet_prefix,
    truncate,
    utc_now_iso,
    years_between,
)

# A resume bullet longer than this is a parsing failure, not a bullet.
MAX_BULLET_CHARS = 1200
# Job descriptions are embedded and sent to LLMs; cap them to bound cost and memory.
MAX_DESCRIPTION_CHARS = 60_000
# Sanity ceiling for a human career, used to reject misparsed dates.
MAX_CAREER_YEARS = 60.0

# Boards supported by the `python-jobspy` backend used in Phase 2.
SUPPORTED_JOB_BOARDS = ("linkedin", "indeed", "glassdoor", "zip_recruiter", "google", "bayt", "naukri", "bdjobs")

FactCategory = Literal["metric", "deployment", "scale", "revenue", "tenure", "award"]

# Separator between the several metrics one locked fact can carry. A comma cannot
# be used: thousands separators are commas too, so "15,000+ users" was split into
# "15" and "000+ users", and every check that looked for the original figure then
# failed against fragments that never appeared in the resume.
METRIC_SEPARATOR = "; "


def split_metric_values(value: Optional[str]) -> List[str]:
    """Split a stored `metric_value` into its individual metrics.

    Falls back to comma separation for profiles sealed before the separator
    changed, but only on ", " — a thousands separator never has a space after it.
    """
    if not value:
        return []
    text = str(value)
    parts = text.split(";") if ";" in text else text.split(", ")
    return [part.strip() for part in parts if part.strip()]


class StrictModel(BaseModel):
    """Base model that rejects unknown fields and validates on assignment.

    `extra="forbid"` turns a renamed or misspelled key from an LLM response into a
    loud validation error instead of a silently dropped field.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True, str_strip_whitespace=True)


# ==============================================================================
# CANDIDATE PROFILE SCHEMA (profile.json)
# ==============================================================================

class ContactInfo(StrictModel):
    """Candidate personal and contact information."""

    full_name: str = Field(..., min_length=2, max_length=120, description="Candidate legal or preferred full name")
    email: EmailStr = Field(..., description="Contact email address")
    phone: Optional[str] = Field(None, description="Contact phone number with country code")
    location: Optional[str] = Field(
        None, max_length=160,
        description="Primary location (e.g. 'San Francisco, CA'); omitted when the resume does not state one",
    )
    linkedin_url: Optional[str] = Field(None, description="LinkedIn profile URL")
    github_url: Optional[str] = Field(None, description="GitHub profile URL")
    portfolio_url: Optional[str] = Field(None, description="Personal portfolio or website URL")

    @field_validator("full_name", mode="before")
    @classmethod
    def _clean_required_text(cls, value: Any) -> str:
        return clean_text(value)

    @field_validator("location", mode="before")
    @classmethod
    def _clean_location(cls, value: Any) -> Optional[str]:
        cleaned = clean_text(value)
        return cleaned or None

    @field_validator("phone", mode="before")
    @classmethod
    def _clean_phone(cls, value: Any) -> Optional[str]:
        return normalize_phone(value)

    @field_validator("linkedin_url", "github_url", "portfolio_url", mode="before")
    @classmethod
    def _clean_urls(cls, value: Any) -> Optional[str]:
        return normalize_url(value)


class WorkAuthorization(StrictModel):
    """Work eligibility and immigration status.

    Drives the answers given to screening questions during auto-apply, so an
    inaccurate value here produces an inaccurate job application.
    """

    citizenship: List[str] = Field(default_factory=list, description="Countries of citizenship")
    current_country: str = Field(..., min_length=2, max_length=80, description="Country of current residence")
    authorized_countries: List[str] = Field(
        default_factory=list,
        description="Countries the candidate may work in without sponsorship",
    )
    requires_sponsorship: bool = Field(
        default=False,
        description="Whether the candidate will now or in the future require visa sponsorship",
    )
    visa_status: Optional[str] = Field(None, max_length=80, description="Current visa type (e.g. 'H-1B', 'OPT', 'F-1')")

    @field_validator("current_country", mode="before")
    @classmethod
    def _clean_country(cls, value: Any) -> str:
        return clean_text(value)

    @field_validator("citizenship", "authorized_countries", mode="before")
    @classmethod
    def _clean_country_lists(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return dedupe_preserving_order(value)

    @field_validator("visa_status", mode="before")
    @classmethod
    def _clean_visa(cls, value: Any) -> Optional[str]:
        cleaned = clean_text(value)
        return cleaned or None

    @model_validator(mode="after")
    def _ensure_home_country_authorized(self) -> WorkAuthorization:
        """A candidate is assumed authorized in their country of residence.

        Without this, a profile that omits `authorized_countries` would answer "no"
        to every location eligibility question on an application form.
        """
        if not self.authorized_countries and self.current_country:
            object.__setattr__(self, "authorized_countries", [self.current_country])
        return self

    def is_authorized_in(self, location: str) -> Optional[bool]:
        """Whether the candidate is work-authorized for a location, or None if unknown.

        Returns None rather than guessing when the location does not clearly name a
        country the profile knows about, so callers can defer to a human.
        """
        haystack = clean_text(location).casefold()
        if not haystack:
            return None
        for country in self.authorized_countries:
            if country.casefold() in haystack:
                return True
        # Common shorthands for the countries most job boards use.
        aliases = {
            "united states": ("usa", "u.s.", "us ", ", us", "america"),
            "united kingdom": ("uk", "england", "scotland", "wales"),
        }
        for country in self.authorized_countries:
            for alias in aliases.get(country.casefold(), ()):
                if alias in haystack:
                    return True
        return None


class Education(StrictModel):
    """Educational qualification."""

    institution: str = Field(..., min_length=2, max_length=160, description="Name of university or college")
    degree: str = Field(..., min_length=1, max_length=80, description="Degree type (e.g. 'B.S.', 'M.S.', 'Ph.D.')")
    field_of_study: str = Field(..., min_length=1, max_length=160, description="Major / field of study")
    start_date: Optional[str] = Field(None, description="Start date (YYYY-MM or YYYY)")
    end_date: Optional[str] = Field(None, description="Graduation or expected date (YYYY-MM or YYYY)")
    gpa: Optional[str] = Field(None, max_length=20, description="GPA or grade score if listed")
    honors: List[str] = Field(default_factory=list, description="Academic honors or dean's list")

    @field_validator("institution", "degree", "field_of_study", mode="before")
    @classmethod
    def _clean_text_fields(cls, value: Any) -> str:
        return clean_text(value)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _normalize_dates(cls, value: Any) -> Optional[str]:
        return normalize_date_string(value)

    @field_validator("gpa", mode="before")
    @classmethod
    def _clean_gpa(cls, value: Any) -> Optional[str]:
        cleaned = clean_text(value)
        if not cleaned or cleaned.casefold() in {"n/a", "none", "null"}:
            return None
        return cleaned

    @field_validator("honors", mode="before")
    @classmethod
    def _clean_honors(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return dedupe_preserving_order(value)

    @model_validator(mode="after")
    def _check_chronology(self) -> Education:
        """Reject a graduation date that precedes enrolment, which signals a misparse."""
        start, end = parse_partial_date(self.start_date), parse_partial_date(self.end_date)
        if start and end and end < start:
            raise ValueError(
                f"Education end_date ({self.end_date}) precedes start_date ({self.start_date}) "
                f"for {self.institution}"
            )
        return self


class LockedFact(StrictModel):
    """Atomic verifiable fact containing an immutable metric, percentage, or milestone.

    Downstream LLM tailoring is strictly barred from modifying these values; the
    rewriter verifies each one survives tailoring and restores it if it does not.
    """

    category: FactCategory = Field(default="metric", description="Kind of achievement this fact records")
    statement: str = Field(..., min_length=5, max_length=MAX_BULLET_CHARS, description="Verbatim factual statement")
    metric_value: Optional[str] = Field(None, max_length=200, description="Extracted metric(s), comma separated")

    @field_validator("category", mode="before")
    @classmethod
    def _coerce_category(cls, value: Any) -> str:
        """Map an unrecognized LLM-supplied category onto the generic 'metric' bucket.

        The category is descriptive metadata only; rejecting the whole fact over it
        would discard a real achievement, which is the worse failure.
        """
        cleaned = clean_text(value).casefold()
        return cleaned if cleaned in {"metric", "deployment", "scale", "revenue", "tenure", "award"} else "metric"

    @field_validator("statement", mode="before")
    @classmethod
    def _clean_statement(cls, value: Any) -> str:
        return truncate(strip_bullet_prefix(clean_text(value)), MAX_BULLET_CHARS)

    @field_validator("metric_value", mode="before")
    @classmethod
    def _clean_metric(cls, value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, (list, tuple)):
            value = METRIC_SEPARATOR.join(str(item) for item in value)
        cleaned = clean_text(value)
        return cleaned or None

    def metrics(self) -> List[str]:
        """Split `metric_value` into the individual metrics it records."""
        return split_metric_values(self.metric_value)


class WorkExperience(StrictModel):
    """Professional work experience entry."""

    company: str = Field(..., min_length=1, max_length=160, description="Company or organization name")
    title: str = Field(..., min_length=1, max_length=160, description="Job title / role")
    location: Optional[str] = Field(None, max_length=160, description="Job location or 'Remote'")
    start_date: str = Field(..., min_length=4, description="Start date (YYYY-MM or YYYY)")
    end_date: Optional[str] = Field(None, description="End date (YYYY-MM or YYYY) or 'Present'")
    is_current: bool = Field(default=False, description="Whether the candidate is currently employed here")
    description_bullets: List[str] = Field(
        default_factory=list,
        description="Baseline resume bullet points describing duties and achievements",
    )
    locked_facts: List[LockedFact] = Field(
        default_factory=list,
        description="Locked career facts and quantifiable achievements extracted from this role",
    )

    @field_validator("company", "title", mode="before")
    @classmethod
    def _clean_required(cls, value: Any) -> str:
        return clean_text(value)

    @field_validator("location", mode="before")
    @classmethod
    def _clean_location(cls, value: Any) -> Optional[str]:
        cleaned = clean_text(value)
        return cleaned or None

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _normalize_dates(cls, value: Any) -> Optional[str]:
        return normalize_date_string(value)

    @field_validator("description_bullets", mode="before")
    @classmethod
    def _clean_bullets(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        cleaned = (truncate(strip_bullet_prefix(clean_text(item)), MAX_BULLET_CHARS) for item in value)
        return dedupe_preserving_order(item for item in cleaned if len(item) >= 3)

    @model_validator(mode="after")
    def _reconcile_tenure(self) -> WorkExperience:
        """Keep `is_current` and `end_date` consistent, and reject inverted ranges."""
        if self.end_date is None or is_present_token(self.end_date):
            object.__setattr__(self, "end_date", "Present")
            object.__setattr__(self, "is_current", True)
        elif self.is_current:
            # An explicit past end date wins over a stale is_current flag.
            object.__setattr__(self, "is_current", False)

        start, end = parse_partial_date(self.start_date), parse_partial_date(self.end_date)
        if start and end and end < start:
            raise ValueError(
                f"Experience end_date ({self.end_date}) precedes start_date ({self.start_date}) "
                f"at {self.company}"
            )
        return self

    @property
    def key(self) -> Tuple[str, str, str]:
        """Stable identity for a role.

        Company alone is not unique: a candidate promoted within the same employer
        has two entries, and keying tailored bullets by company alone merges them.
        """
        return (self.company.casefold(), self.title.casefold(), (self.start_date or "").casefold())

    def date_interval(self) -> Optional[Tuple[date, date]]:
        """Resolve this role to a concrete [start, end] span for tenure arithmetic."""
        start = parse_partial_date(self.start_date)
        if start is None:
            return None
        end = date.today() if self.is_current else parse_partial_date(self.end_date)
        if end is None:
            end = date.today()
        return (start, max(start, end))


class Project(StrictModel):
    """Key personal or open-source project."""

    title: str = Field(..., min_length=1, max_length=160, description="Project name")
    role: Optional[str] = Field(None, max_length=120, description="Role in project (e.g. 'Lead Developer')")
    technologies: List[str] = Field(default_factory=list, description="Technologies / stack used")
    description: str = Field(..., min_length=3, max_length=MAX_BULLET_CHARS, description="Summary of the project")
    link: Optional[str] = Field(None, description="URL to repository or live demo")
    locked_facts: List[LockedFact] = Field(
        default_factory=list,
        description="Verifiable performance or scale metrics achieved by the project",
    )

    @field_validator("title", mode="before")
    @classmethod
    def _clean_title(cls, value: Any) -> str:
        return clean_text(value)

    @field_validator("role", mode="before")
    @classmethod
    def _clean_role(cls, value: Any) -> Optional[str]:
        cleaned = clean_text(value)
        return cleaned or None

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value: Any) -> str:
        return truncate(strip_bullet_prefix(clean_text(value)), MAX_BULLET_CHARS)

    @field_validator("technologies", mode="before")
    @classmethod
    def _clean_technologies(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        return dedupe_preserving_order(value)

    @field_validator("link", mode="before")
    @classmethod
    def _clean_link(cls, value: Any) -> Optional[str]:
        return normalize_url(value)


class SkillSet(StrictModel):
    """Categorized technical and professional skills."""

    languages: List[str] = Field(default_factory=list, description="Programming languages")
    frameworks: List[str] = Field(default_factory=list, description="Frameworks and libraries")
    developer_tools: List[str] = Field(default_factory=list, description="Databases, git, docker, dev tools")
    cloud_devops: List[str] = Field(default_factory=list, description="AWS, GCP, Azure, Kubernetes, CI/CD")
    domain_knowledge: List[str] = Field(default_factory=list, description="System design, microservices, ML, etc.")

    @field_validator("languages", "frameworks", "developer_tools", "cloud_devops", "domain_knowledge", mode="before")
    @classmethod
    def _clean_skill_lists(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            # LLMs sometimes return a comma-joined string instead of a list.
            value = [part for part in re.split(r"[,;|]", value)]
        return dedupe_preserving_order(value)

    def all_skills(self) -> List[str]:
        """Every skill across all categories, de-duplicated, original casing preserved."""
        return dedupe_preserving_order(
            self.languages + self.frameworks + self.developer_tools + self.cloud_devops + self.domain_knowledge
        )


class Certification(StrictModel):
    """Professional certification or license."""

    name: str = Field(..., min_length=2, max_length=200, description="Certification name")
    issuer: str = Field(..., min_length=1, max_length=160, description="Issuing organization")
    issue_date: Optional[str] = Field(None, description="Date issued")
    credential_url: Optional[str] = Field(None, description="Verification URL or license ID")

    @field_validator("name", "issuer", mode="before")
    @classmethod
    def _clean_required(cls, value: Any) -> str:
        return clean_text(value)

    @field_validator("issue_date", mode="before")
    @classmethod
    def _normalize_issue_date(cls, value: Any) -> Optional[str]:
        return normalize_date_string(value)

    @field_validator("credential_url", mode="before")
    @classmethod
    def _clean_url(cls, value: Any) -> Optional[str]:
        return normalize_url(value)


class CandidateProfile(StrictModel):
    """Strict, sealed candidate profile (profile.json).

    Single source of truth across all six pipeline phases. Nothing downstream may
    assert a fact about the candidate that does not originate here.
    """

    contact: ContactInfo
    summary: str = Field(..., min_length=10, max_length=3000, description="Professional overview statement")
    work_authorization: WorkAuthorization
    education: List[Education] = Field(default_factory=list)
    experience: List[WorkExperience] = Field(default_factory=list)
    skills: SkillSet
    projects: List[Project] = Field(default_factory=list)
    certifications: List[Certification] = Field(default_factory=list)
    years_of_experience: float = Field(
        ..., ge=0.0, le=MAX_CAREER_YEARS, description="Total professional experience in years"
    )
    desired_salary: Optional[int] = Field(
        default=None, ge=0, le=10_000_000,
        description="Salary expectation used to answer compensation questions; omitted when unknown",
    )

    # Provenance and integrity
    source_document: Optional[str] = Field(
        default=None, description="Filename of the resume this profile was extracted from"
    )
    extraction_method: Optional[str] = Field(
        default=None, description="Extractor that produced this profile (e.g. 'openai:gpt-4o', 'deterministic')"
    )
    fact_hash: Optional[str] = Field(
        default=None, description="SHA-256 integrity hash over all locked facts"
    )
    last_updated: str = Field(default_factory=utc_now_iso, description="Timestamp of profile digitization")

    @field_validator("summary", mode="before")
    @classmethod
    def _clean_summary(cls, value: Any) -> str:
        return truncate(clean_text(value), 3000)

    @field_validator("fact_hash", mode="before")
    @classmethod
    def _validate_hash(cls, value: Any) -> Optional[str]:
        """Reject a malformed seal outright rather than letting it fail verification later."""
        if value is None:
            return None
        cleaned = clean_text(value).lower()
        if not cleaned:
            return None
        if not re.fullmatch(r"[0-9a-f]{64}", cleaned):
            raise ValueError("fact_hash must be a 64-character lowercase SHA-256 hex digest")
        return cleaned

    # --- Derived values -------------------------------------------------------

    def computed_years_of_experience(self) -> Optional[float]:
        """Years of experience derived from role dates, ignoring overlapping roles.

        Returns None when no role carries a parseable start date.
        """
        intervals = [item for item in (exp.date_interval() for exp in self.experience) if item]
        if not intervals:
            return None
        return years_between(intervals)

    def experience_discrepancy(self) -> Optional[float]:
        """Absolute gap between the stated and the date-derived years of experience.

        A large gap means either the resume's dates or its stated total is wrong;
        `verify_profile` surfaces it rather than silently trusting one of them.
        """
        computed = self.computed_years_of_experience()
        if computed is None:
            return None
        return round(abs(computed - self.years_of_experience), 1)

    def all_locked_facts(self) -> List[LockedFact]:
        """Every locked fact across experience and projects."""
        facts: List[LockedFact] = []
        for exp in self.experience:
            facts.extend(exp.locked_facts)
        for proj in self.projects:
            facts.extend(proj.locked_facts)
        return facts

    def all_locked_metrics(self) -> List[str]:
        """Every individual metric string recorded in a locked fact."""
        metrics: List[str] = []
        for fact in self.all_locked_facts():
            metrics.extend(fact.metrics())
        return dedupe_preserving_order(metrics, casefold=False)

    # --- Integrity seal -------------------------------------------------------

    def compute_fact_hash(self) -> str:
        """SHA-256 over every locked career fact, order-independent."""
        collected: List[str] = []
        for exp in self.experience:
            for fact in exp.locked_facts:
                collected.append(f"{exp.company}:{fact.category}:{fact.statement}:{fact.metric_value or ''}")
        for proj in self.projects:
            for fact in proj.locked_facts:
                collected.append(f"{proj.title}:{fact.category}:{fact.statement}:{fact.metric_value or ''}")
        collected.sort()
        return hashlib.sha256("||".join(collected).encode("utf-8")).hexdigest()

    def seal_profile(self) -> CandidateProfile:
        """Seal the profile by computing and setting the fact hash."""
        self.fact_hash = self.compute_fact_hash()
        self.last_updated = utc_now_iso()
        return self

    def verify_integrity(self) -> bool:
        """Whether the locked facts still match the seal."""
        if not self.fact_hash:
            return False
        return self.compute_fact_hash() == self.fact_hash


# ==============================================================================
# SEARCH PARAMETERS SCHEMA (searches.yaml)
# ==============================================================================

class SearchParameters(StrictModel):
    """Deterministic configuration for job sourcing (searches.yaml)."""

    target_domains: List[str] = Field(
        default_factory=lambda: ["Software Engineer"],
        min_length=1,
        description="Target job titles / domains",
    )
    desired_experience_years: float = Field(
        default=3.0, ge=0.0, le=MAX_CAREER_YEARS, description="Desired years of experience for level matching"
    )
    locations: List[str] = Field(
        default_factory=lambda: ["Remote"], min_length=1, description="Target cities, states, or 'Remote'"
    )
    is_remote: bool = Field(default=True, description="Whether only remote jobs should be kept")
    hours_old: int = Field(
        default=48, ge=1, le=8760, description="Maximum posting age in hours (1 hour to 1 year)"
    )
    job_boards: List[str] = Field(
        default_factory=lambda: ["linkedin", "indeed", "glassdoor", "zip_recruiter"],
        min_length=1,
        description="Platforms to scrape via JobSpy",
    )
    country_indeed: str = Field(
        default="usa",
        description="Country used by the Indeed and Glassdoor backends (e.g. 'usa', 'india', 'uk')",
    )
    min_salary: Optional[int] = Field(
        default=None, ge=0, le=10_000_000, description="Minimum annual salary threshold (optional)"
    )
    max_results_per_board: int = Field(
        default=25, ge=1, le=200, description="Limit on jobs to ingest per board per search term"
    )
    ats_companies: Dict[str, List[str]] = Field(
        default_factory=dict,
        description="Direct ATS boards to poll, keyed by provider ('greenhouse', 'lever', 'ashby')",
    )
    proxy_url: Optional[str] = Field(default=None, description="Residential proxy string, overriding .env")

    @field_validator("target_domains", "locations", mode="before")
    @classmethod
    def _clean_string_lists(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [part for part in re.split(r"[,;]", value)]
        return dedupe_preserving_order(value)

    @field_validator("job_boards", mode="before")
    @classmethod
    def _validate_boards(cls, value: Any) -> List[str]:
        """Normalize board names and reject any the scraper backend cannot service.

        Failing here beats failing mid-sweep: a typo like 'ziprecruiter' would
        otherwise just silently return zero jobs from that board.
        """
        if value is None:
            return []
        if isinstance(value, str):
            value = [part for part in re.split(r"[,;]", value)]
        boards = [clean_text(item).lower().replace("-", "_").replace(" ", "_") for item in value]
        boards = [board for board in boards if board]
        # Accept the common spelling variant rather than rejecting it.
        boards = ["zip_recruiter" if board == "ziprecruiter" else board for board in boards]
        unknown = sorted({board for board in boards if board not in SUPPORTED_JOB_BOARDS})
        if unknown:
            raise ValueError(
                f"Unsupported job board(s): {', '.join(unknown)}. "
                f"Supported: {', '.join(SUPPORTED_JOB_BOARDS)}"
            )
        return dedupe_preserving_order(boards)

    @field_validator("country_indeed", mode="before")
    @classmethod
    def _clean_country(cls, value: Any) -> str:
        return clean_text(value).lower() or "usa"

    @field_validator("ats_companies", mode="before")
    @classmethod
    def _validate_ats(cls, value: Any) -> Dict[str, List[str]]:
        """Normalize the direct-ATS registry and reject unsupported providers."""
        if not value:
            return {}
        if not isinstance(value, dict):
            raise ValueError("ats_companies must be a mapping of provider -> list of board tokens")
        supported = {"greenhouse", "lever", "ashby"}
        result: Dict[str, List[str]] = {}
        for provider, companies in value.items():
            key = clean_text(provider).lower()
            if key not in supported:
                raise ValueError(
                    f"Unsupported ATS provider '{provider}'. Supported: {', '.join(sorted(supported))}"
                )
            if isinstance(companies, str):
                companies = [part for part in re.split(r"[,;]", companies)]
            tokens = dedupe_preserving_order(str(item).lower() for item in (companies or []))
            if tokens:
                result[key] = tokens
        return result

    @field_validator("proxy_url", mode="before")
    @classmethod
    def _validate_proxy(cls, value: Any) -> Optional[str]:
        """Accept a proxy only in a form both requests and Playwright understand."""
        cleaned = clean_text(value)
        if not cleaned:
            return None
        if not cleaned.lower().startswith(("http://", "https://", "socks5://")):
            cleaned = f"http://{cleaned}"
        if not re.match(r"^(https?|socks5)://([^:@/\s]+(:[^@/\s]*)?@)?[^:/\s]+(:\d{1,5})?$", cleaned):
            raise ValueError(
                f"Invalid proxy URL '{value}'. Expected scheme://[user:pass@]host[:port]"
            )
        return cleaned


# ==============================================================================
# SOURCED JOB SCHEMA (Phase 2 output / downstream input)
# ==============================================================================

class JobPosting(StrictModel):
    """Normalized, deduplicated job posting from JobSpy or a direct ATS feed."""

    id: str = Field(..., min_length=1, max_length=64, description="Deterministic identifier")
    title: str = Field(..., min_length=1, max_length=300, description="Standardized job title")
    company: str = Field(..., min_length=1, max_length=200, description="Hiring company or organization")
    location: str = Field(default="Remote", max_length=200, description="Job location or Remote")
    job_url: str = Field(..., description="Direct application URL or portal link")
    description: str = Field(default="", description="Complete job description text")
    date_posted: Optional[str] = Field(None, description="Date or timestamp posted")
    is_remote: bool = Field(default=False, description="Remote work indicator")
    salary_min: Optional[float] = Field(None, ge=0, description="Minimum salary if listed")
    salary_max: Optional[float] = Field(None, ge=0, description="Maximum salary if listed")
    salary_currency: Optional[str] = Field("USD", max_length=8, description="Currency for listed salary")
    job_type: Optional[str] = Field(None, max_length=60, description="Employment type (fulltime, contract, ...)")
    source: str = Field(..., min_length=1, max_length=40, description="Discovery source")
    discovered_at: str = Field(default_factory=utc_now_iso, description="Timestamp when listing was discovered")

    @field_validator("title", "company", mode="before")
    @classmethod
    def _clean_required(cls, value: Any) -> str:
        return clean_text(value)

    @field_validator("id", mode="before")
    @classmethod
    def _validate_id(cls, value: Any) -> str:
        """IDs are used as filename components and SQLite keys, so keep them opaque."""
        cleaned = clean_text(value)
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,64}", cleaned):
            raise ValueError(
                f"Job id must be 1-64 characters of [A-Za-z0-9_.-], got {value!r}"
            )
        return cleaned

    @field_validator("location", mode="before")
    @classmethod
    def _clean_location(cls, value: Any) -> str:
        return clean_text(value) or "Remote"

    @field_validator("job_url", mode="before")
    @classmethod
    def _validate_job_url(cls, value: Any) -> str:
        """A posting without a usable URL cannot be applied to, so reject it here."""
        normalized = normalize_url(value)
        if not normalized:
            raise ValueError(f"job_url must be a valid http(s) URL, got {value!r}")
        return normalized

    @field_validator("description", mode="before")
    @classmethod
    def _clean_description(cls, value: Any) -> str:
        return truncate(clean_text(value), MAX_DESCRIPTION_CHARS)

    @field_validator("source", mode="before")
    @classmethod
    def _clean_source(cls, value: Any) -> str:
        return clean_text(value).lower()

    @field_validator("job_type", mode="before")
    @classmethod
    def _clean_job_type(cls, value: Any) -> Optional[str]:
        cleaned = clean_text(value).lower()
        if not cleaned or cleaned in {"nan", "none", "null"}:
            return None
        return cleaned

    @field_validator("salary_currency", mode="before")
    @classmethod
    def _clean_currency(cls, value: Any) -> Optional[str]:
        cleaned = clean_text(value).upper()
        return cleaned or None

    @field_validator("date_posted", mode="before")
    @classmethod
    def _clean_date_posted(cls, value: Any) -> Optional[str]:
        cleaned = clean_text(value)
        if not cleaned or cleaned.lower() in {"nat", "nan", "none", "null"}:
            return None
        return cleaned

    @model_validator(mode="after")
    def _reconcile_derived_fields(self) -> JobPosting:
        """Repair inverted salary bands and infer remoteness from the text."""
        if self.salary_min is not None and self.salary_max is not None and self.salary_min > self.salary_max:
            object.__setattr__(self, "salary_min", self.salary_max)
            object.__setattr__(self, "salary_max", self.salary_min)
        if not self.is_remote:
            haystack = f"{self.title} {self.location}".casefold()
            if "remote" in haystack or "work from home" in haystack:
                object.__setattr__(self, "is_remote", True)
        return self

    @classmethod
    def create_id(cls, job_url: str, company: str, title: str) -> str:
        """Generate a deterministic 16-char hex ID from the URL, or company+title.

        URL query strings are stripped first: the same Greenhouse posting is served
        with per-session tracking parameters, and keying on them would defeat the
        delta store's deduplication.
        """
        cleaned_url = clean_text(job_url)
        if cleaned_url:
            token = re.sub(r"[?#].*$", "", cleaned_url).rstrip("/").lower()
        else:
            token = f"{clean_text(company).lower()}:{clean_text(title).lower()}"
        return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]

    def posted_at(self) -> Optional[datetime]:
        """The posting timestamp as an aware UTC datetime, when parseable."""
        from job_agent.config.normalize import parse_posting_timestamp

        return parse_posting_timestamp(self.date_posted)

    def age_hours(self) -> Optional[float]:
        """How many hours ago this was posted, or None when the date is unknown."""
        posted = self.posted_at()
        if posted is None:
            return None
        delta = datetime.now(timezone.utc) - posted
        return max(0.0, delta.total_seconds() / 3600.0)


# ==============================================================================
# SEMANTIC EVALUATION SCHEMA (Phase 3 output / downstream input)
# ==============================================================================

DEFAULT_MATCH_THRESHOLD = 7.0


class RerankerVerdict(StrictModel):
    """Schema an LLM re-ranker response must satisfy before it is trusted.

    Validating the model's JSON against this before use is what stops a
    hallucinated `"fit_score": 95` or a missing field from corrupting the ranking.
    """

    fit_score: float = Field(..., ge=1.0, le=10.0)
    technical_score: float = Field(default=0.0, ge=0.0, le=10.0)
    seniority_score: float = Field(default=0.0, ge=0.0, le=10.0)
    reasoning: str = Field(..., min_length=1, max_length=4000)
    matching_skills: List[str] = Field(default_factory=list)
    missing_skills: List[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    @field_validator("fit_score", "technical_score", "seniority_score", mode="before")
    @classmethod
    def _coerce_score(cls, value: Any) -> float:
        """Accept '8.5/10' and percentage-style scores, then clamp into range.

        Models frequently answer on the wrong scale; clamping keeps a usable
        ordering instead of discarding the evaluation entirely.
        """
        if value is None:
            return 0.0
        if isinstance(value, str):
            match = re.search(r"-?\d+(?:\.\d+)?", value)
            if not match:
                raise ValueError(f"Could not read a numeric score from {value!r}")
            value = float(match.group(0))
        score = float(value)
        if score > 10.0:
            # A 0-100 answer rescales cleanly; anything else clamps to the ceiling.
            score = score / 10.0 if score <= 100.0 else 10.0
        return round(max(0.0, min(10.0, score)), 2)

    @field_validator("reasoning", mode="before")
    @classmethod
    def _clean_reasoning(cls, value: Any) -> str:
        return truncate(clean_text(value), 4000) or "No reasoning provided."

    @field_validator("matching_skills", "missing_skills", mode="before")
    @classmethod
    def _clean_skills(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = [part for part in re.split(r"[,;]", value)]
        return dedupe_preserving_order(value)[:25]

    @model_validator(mode="after")
    def _clamp_fit_score(self) -> RerankerVerdict:
        """The public scale starts at 1.0; a 0.0 from the model means 'worst', not 'unscored'."""
        if self.fit_score < 1.0:
            object.__setattr__(self, "fit_score", 1.0)
        return self


class EvaluationScore(StrictModel):
    """Two-tier semantic match evaluation result."""

    embedding_similarity: float = Field(..., ge=0.0, le=1.0, description="Tier 1 cosine similarity")
    fit_score: float = Field(..., ge=1.0, le=10.0, description="Tier 2 normalized fit score")
    technical_score: float = Field(default=0.0, ge=0.0, le=10.0, description="Technical stack alignment")
    seniority_score: float = Field(default=0.0, ge=0.0, le=10.0, description="Seniority / experience alignment")
    threshold_used: float = Field(
        default=DEFAULT_MATCH_THRESHOLD, ge=0.0, le=10.0, description="Cutoff this score was judged against"
    )
    passed_threshold: bool = Field(default=False, description="Whether the job met the cutoff")
    reasoning: str = Field(..., min_length=1, max_length=4000, description="Justification for the score")
    matching_skills: List[str] = Field(default_factory=list, description="Verified overlapping skills")
    missing_skills: List[str] = Field(default_factory=list, description="Requirements absent from the profile")
    scored_by: str = Field(default="heuristic", max_length=60, description="Judge that produced this score")
    evaluated_at: str = Field(default_factory=utc_now_iso, description="Evaluation timestamp")

    @field_validator("embedding_similarity", mode="before")
    @classmethod
    def _clamp_similarity(cls, value: Any) -> float:
        return round(max(0.0, min(1.0, float(value))), 4)

    @field_validator("fit_score", "technical_score", "seniority_score", mode="before")
    @classmethod
    def _coerce_score(cls, value: Any) -> float:
        return RerankerVerdict._coerce_score(value)

    @field_validator("reasoning", mode="before")
    @classmethod
    def _clean_reasoning(cls, value: Any) -> str:
        return truncate(clean_text(value), 4000) or "No reasoning provided."

    @field_validator("matching_skills", "missing_skills", mode="before")
    @classmethod
    def _clean_skills(cls, value: Any) -> List[str]:
        return RerankerVerdict._clean_skills(value)

    @model_validator(mode="after")
    def _enforce_threshold_consistency(self) -> EvaluationScore:
        """Derive `passed_threshold` from the score rather than trusting the model.

        An LLM that reports `fit_score: 4.0` alongside `passed_threshold: true`
        would otherwise push an unqualified role into the auto-apply queue.
        """
        if self.fit_score < 1.0:
            object.__setattr__(self, "fit_score", 1.0)
        object.__setattr__(self, "passed_threshold", self.fit_score >= self.threshold_used)
        return self


class EvaluatedJob(StrictModel):
    """Pairing of a job posting with its two-tier evaluation results."""

    job: JobPosting
    evaluation: EvaluationScore


# ==============================================================================
# TAILORING, APPLICATION, AND TRACKING ARTIFACTS (Phases 4-6)
# ==============================================================================

class TailoredResumeRecord(StrictModel):
    """One entry in `tailored_resumes/manifest.json`."""

    job_id: str = Field(..., min_length=1, max_length=64)
    title: str
    company: str
    score: float = Field(..., ge=0.0, le=10.0)
    pdf_path: str
    json_path: str
    restored_metrics: List[str] = Field(
        default_factory=list, description="Locked metrics the tailorer had to restore after rewriting"
    )
    dropped_fabrications: List[str] = Field(
        default_factory=list, description="Invented metrics removed from the rewritten bullets"
    )
    tailored_at: str = Field(default_factory=utc_now_iso)

    model_config = ConfigDict(extra="ignore", validate_assignment=True, str_strip_whitespace=True)


class ApplicationOutcome(StrictModel):
    """Result of one auto-apply attempt."""

    job_id: str = Field(..., min_length=1, max_length=64)
    title: str
    company: str
    job_url: str
    status: Literal["applied", "failed", "dry_run", "skipped"] = "failed"
    applied: bool = False
    steps_taken: int = Field(default=0, ge=0)
    fit_score: Optional[float] = Field(default=None, ge=0.0, le=10.0)
    error: Optional[str] = None
    pdf_path: Optional[str] = None
    finished_at: str = Field(default_factory=utc_now_iso)

    model_config = ConfigDict(extra="ignore", validate_assignment=True, str_strip_whitespace=True)

    @model_validator(mode="after")
    def _reconcile_status(self) -> ApplicationOutcome:
        """`applied` is true only for a confirmed submission, never for a dry run."""
        object.__setattr__(self, "applied", self.status == "applied")
        return self
