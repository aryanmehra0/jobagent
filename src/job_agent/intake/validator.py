"""Validation, fact locking, and integrity enforcement for the candidate profile.

Three jobs, in order:

1. **Enrich** — scan every bullet for quantifiable achievements and register them
   as `LockedFact` entries so later stages know exactly which numbers are load-bearing.
2. **Fact-check** — when the source resume text is available, verify that every
   locked metric actually appears in it. Metrics an LLM invented are dropped here,
   before they get sealed and printed onto a tailored PDF.
3. **Seal** — validate against the schema, compute the SHA-256 fact hash, and persist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console

from job_agent.config.normalize import clean_text
from job_agent.config.schema import (
    METRIC_SEPARATOR,
    CandidateProfile,
    split_metric_values,
)

console = Console()

# Magnitude words, including the Indian units. Omitting "crore" and "lakh" was not
# a missed match but a corruption: "₹45+ crore" was captured as "₹45", understating
# a real achievement by seven orders of magnitude on the candidate's own resume.
_MAGNITUDE = r"(?:k|m|b|bn|mn|million|billion|trillion|thousand|crore|cr|lakh|lac)"
_NUMBER = r"\d+(?:,\d{2,3})*(?:\.\d+)?"
_CURRENCY = r"[$€£₹¥]"

# Ordered longest-first: overlapping matches are resolved by preferring the widest
# span, so "₹45+ crore" wins over the bare "₹45" inside it.
METRIC_PATTERNS = [
    # Currency, with an optional magnitude word: $2.5M, ₹45+ crore, £95k
    rf"{_CURRENCY}\s?{_NUMBER}\s*\+?\s*{_MAGNITUDE}?\b",
    # Percentages, keeping a trailing "+": 42%, 99.99%, 92%+
    rf"\b{_NUMBER}%\+?",
    # Multipliers: 10x, 3.5x
    rf"\b{_NUMBER}x\b",
    # Tenure: 5+ years
    rf"\b{_NUMBER}\+?\s*years?\b",
    # A magnitude-suffixed count, with the noun it qualifies: 100k requests, 3.28M+ records
    rf"\b{_NUMBER}\s*{_MAGNITUDE}\+?(?:\s+[a-z][\w-]*){{0,2}}\b",
    # "N+" is an explicit claim, so trust the noun that follows: 50+ parameter, 500+ SMBs
    rf"\b{_NUMBER}\+\s*(?:[a-z][\w-]*\s+){{0,2}}[A-Za-z][\w-]*\b",
    # A plain count of a plural thing: 15 nationalized bank partners, 2 manuscripts
    rf"\b{_NUMBER}\s+(?:[a-z][\w-]*\s+){{0,2}}[a-z][\w-]*s\b",
]

_COMPILED_METRIC_PATTERNS = [re.compile(pattern, re.IGNORECASE) for pattern in METRIC_PATTERNS]

# Plain counts that are part of a name or version rather than an achievement.
_NOT_A_METRIC = re.compile(
    r"^\d+(?:\.\d+)?\s*(?:st|nd|rd|th)\b"      # ordinals: "1st place"
    r"|^\d{4}\s"                                 # a year leading the phrase
    r"|^0\s",                                    # "0 downtime" style, no magnitude
    re.IGNORECASE,
)

# A metric phrase ends where the sentence moves on. Cutting at the first
# connective keeps "500+ SMBs" out of "500+ SMBs to define" and "200k RPS" out of
# "200k RPS with", both of which would otherwise fail the tailoring stage's
# exact-substring check once a rewrite reflowed the sentence.
_CONNECTIVE_CUT = re.compile(
    r"\s+(?:with|and|or|to|for|in|of|on|at|by|from|the|a|an|over|across|using|through|"
    r"per|via|into|that|which|while)\b.*$",
    re.IGNORECASE,
)

# Categories inferred from the shape of the metric, so locked facts carry useful labels.
_CATEGORY_RULES: Tuple[Tuple[str, re.Pattern], ...] = (
    ("revenue", re.compile(r"[$€£₹]")),
    ("tenure", re.compile(r"\byears?\b", re.IGNORECASE)),
    ("scale", re.compile(r"\b(?:rps|qps|tps|users|requests|events|transactions|records|nodes)\b", re.IGNORECASE)),
)


def extract_potential_metrics(text: str) -> List[str]:
    """Scan text for numeric metrics, scale indicators, and percentages.

    Overlapping candidates are resolved by keeping the widest span at each
    position. That is what makes "₹45+ crore" survive as a unit instead of being
    truncated to the "₹45" that a narrower pattern also matches.

    Results are returned in the order they appear, de-duplicated, with their
    original formatting preserved, because the tailoring stage later checks the
    rewritten bullets for these exact substrings.
    """
    if not text:
        return []

    spans: List[Tuple[int, int, str]] = []
    for pattern in _COMPILED_METRIC_PATTERNS:
        for match in pattern.finditer(text):
            value = _CONNECTIVE_CUT.sub("", clean_text(match.group(0))).strip(" ,;:")
            if value and not _NOT_A_METRIC.match(value):
                spans.append((match.start(), match.start() + len(value), value))

    # Widest span first at each start, so the longest match claims the region.
    spans.sort(key=lambda item: (item[0], -(item[1] - item[0])))

    found: List[str] = []
    consumed_to = -1
    for start, end, value in spans:
        if start < consumed_to:
            continue
        consumed_to = end
        if value not in found:
            found.append(value)
    return found


def _infer_category(metrics: List[str]) -> str:
    """Label a locked fact by the kind of metric it carries."""
    blob = " ".join(metrics)
    for category, pattern in _CATEGORY_RULES:
        if pattern.search(blob):
            return category
    return "metric"


def enrich_locked_facts(profile_data: Dict[str, Any]) -> Dict[str, Any]:
    """Register every quantifiable achievement in experience and projects as a locked fact.

    Existing facts are preserved; only statements not already locked are added, so
    an LLM that already supplied good `locked_facts` is not second-guessed.
    """
    for exp in profile_data.get("experience") or []:
        existing = {
            clean_text(fact.get("statement"))
            for fact in (exp.get("locked_facts") or [])
            if isinstance(fact, dict)
        }
        for bullet in exp.get("description_bullets") or []:
            statement = clean_text(bullet)
            metrics = extract_potential_metrics(statement)
            if metrics and statement not in existing:
                exp.setdefault("locked_facts", []).append({
                    "category": _infer_category(metrics),
                    "statement": statement,
                    "metric_value": METRIC_SEPARATOR.join(metrics),
                })
                existing.add(statement)

    for proj in profile_data.get("projects") or []:
        existing = {
            clean_text(fact.get("statement"))
            for fact in (proj.get("locked_facts") or [])
            if isinstance(fact, dict)
        }
        statement = clean_text(proj.get("description"))
        metrics = extract_potential_metrics(statement)
        if metrics and statement and statement not in existing:
            proj.setdefault("locked_facts", []).append({
                "category": _infer_category(metrics),
                "statement": statement,
                "metric_value": METRIC_SEPARATOR.join(metrics),
            })

    return profile_data


# ==============================================================================
# SOURCE FACT CHECKING
# ==============================================================================

def _normalize_for_matching(text: str) -> str:
    """Collapse a string so that '$340k', '$ 340 K', and '$340K' all compare equal."""
    return re.sub(r"[\s,]", "", clean_text(text)).lower()


def audit_metrics_against_source(
    profile_data: Dict[str, Any],
    source_text: str,
) -> Tuple[Dict[str, Any], List[str]]:
    """Drop locked-fact metrics that do not appear in the original resume text.

    This is the anti-hallucination gate at intake. A metric an extractor invented
    is removed from `metric_value`; if none of a fact's metrics survive, the fact
    itself is dropped rather than sealed as verified truth.

    Returns:
        The audited profile data, and the list of rejected metric strings.
    """
    if not source_text:
        return profile_data, []

    haystack = _normalize_for_matching(source_text)
    rejected: List[str] = []

    def audit(container: Dict[str, Any]) -> None:
        kept_facts: List[Dict[str, Any]] = []
        for fact in container.get("locked_facts") or []:
            if not isinstance(fact, dict):
                continue
            raw_metric = fact.get("metric_value")
            if not raw_metric:
                kept_facts.append(fact)
                continue
            metrics = split_metric_values(raw_metric)
            verified = [m for m in metrics if _normalize_for_matching(m) in haystack]
            rejected.extend(m for m in metrics if m not in verified)
            if verified:
                fact["metric_value"] = METRIC_SEPARATOR.join(verified)
                kept_facts.append(fact)
            elif metrics:
                # Every number in this fact is unverifiable: do not seal it.
                continue
            else:
                kept_facts.append(fact)
        container["locked_facts"] = kept_facts

    for exp in profile_data.get("experience") or []:
        audit(exp)
    for proj in profile_data.get("projects") or []:
        audit(proj)

    return profile_data, rejected


# ==============================================================================
# VALIDATION, SEALING, AND PERSISTENCE
# ==============================================================================

def validate_and_save_profile(
    profile_data: Dict[str, Any],
    output_path: Path,
    source_text: Optional[str] = None,
) -> CandidateProfile:
    """Validate, fact-check, seal, and write the candidate profile to disk."""
    enriched = enrich_locked_facts(profile_data)

    if source_text:
        enriched, rejected = audit_metrics_against_source(enriched, source_text)
        if rejected:
            console.print(
                f"[bold yellow]Anti-hallucination gate:[/bold yellow] rejected "
                f"{len(rejected)} metric(s) not found in the resume text: "
                f"{', '.join(sorted(set(rejected))[:8])}"
            )

    profile = CandidateProfile(**enriched)
    profile.seal_profile()

    # Surface a disagreement between the stated and the date-derived tenure rather
    # than silently preferring one; both come from the candidate's own document.
    discrepancy = profile.experience_discrepancy()
    if discrepancy is not None and discrepancy >= 1.5:
        console.print(
            f"[yellow]Note:[/yellow] stated experience is {profile.years_of_experience:g} years but the "
            f"role dates add up to {profile.computed_years_of_experience():g}. "
            "Check the dates in your resume, or correct years_of_experience in profile.json."
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")

    console.print(
        f"[bold green]OK[/bold green] Candidate profile sealed with integrity hash: "
        f"[cyan]{profile.fact_hash[:16]}...[/cyan]"
    )
    console.print(f"[bold green]OK[/bold green] Saved to: [yellow]{output_path}[/yellow]")
    return profile


def load_and_verify_profile(profile_path: Path) -> Tuple[CandidateProfile, bool]:
    """Load a profile from disk and verify its cryptographic fact seal."""
    if not profile_path.exists():
        raise FileNotFoundError(
            f"Candidate profile not found at {profile_path}. "
            "Run: python main.py intake --resume <path_to_pdf>"
        )

    try:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Candidate profile at {profile_path} is not valid JSON: {exc}") from exc

    profile = CandidateProfile(**data)
    is_valid = profile.verify_integrity()

    if not is_valid:
        console.print(
            "[bold red]WARNING:[/bold red] Profile integrity verification failed. "
            "Locked facts were modified outside the intake engine. "
            "Re-run 'python main.py intake' to re-seal the profile."
        )
    else:
        console.print("[bold green]OK[/bold green] Profile integrity verified; zero fact mutation detected.")

    return profile, is_valid


def describe_profile_gaps(profile: CandidateProfile) -> List[str]:
    """List fields that are absent and would degrade a later stage.

    Reported by `main.py status` and `main.py doctor` so the user can fix a thin
    profile before it produces a thin resume.
    """
    gaps: List[str] = []
    if not profile.contact.location:
        gaps.append("contact.location is missing (application forms ask for it)")
    if not profile.contact.phone:
        gaps.append("contact.phone is missing (many portals require it)")
    if not profile.experience:
        gaps.append("no work experience was extracted")
    if not profile.skills.all_skills():
        gaps.append("no skills were extracted (Tier 1 matching will be unreliable)")
    if not profile.all_locked_facts():
        gaps.append("no quantifiable achievements were found to lock")
    if profile.work_authorization.current_country in ("", "Unspecified"):
        gaps.append("work_authorization.current_country is unknown (screening answers will be skipped)")
    if profile.desired_salary is None:
        gaps.append("desired_salary is not set (compensation questions will be left blank)")
    return gaps
