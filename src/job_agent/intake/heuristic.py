"""Deterministic, offline resume parser.

This is the extractor used when no LLM API key is configured, and it is also the
cross-check for LLM output. Its one hard rule is that **it never invents a fact**:
every value it returns is either copied from the resume text or derived
arithmetically from values that were (for example, total years of experience from
role dates). A field it cannot find is reported as missing, not filled with a
plausible-looking placeholder.

That rule matters because the profile produced here is consumed verbatim by resume
tailoring, auto-apply form filling, and cold outreach. A placeholder employer or a
fabricated certification does not stay in the JSON; it ends up on a PDF sent to a
real hiring manager.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from job_agent.config.normalize import (
    EMAIL_RE,
    PHONE_RE,
    clean_text,
    dedupe_preserving_order,
    is_present_token,
    looks_like_person_name,
    normalize_date_string,
    normalize_phone,
    normalize_url,
    parse_partial_date,
    strip_bullet_prefix,
    years_between,
)


class ResumeParseError(ValueError):
    """Raised when the resume lacks facts that cannot be responsibly guessed."""

    def __init__(self, missing: List[str], detail: str = ""):
        self.missing = missing
        message = (
            "Could not extract required field(s) from the resume: "
            + ", ".join(missing)
        )
        if detail:
            message += f". {detail}"
        super().__init__(message)


# ==============================================================================
# SECTION SEGMENTATION
# ==============================================================================

SECTION_ALIASES: Dict[str, Tuple[str, ...]] = {
    "summary": ("summary", "professional summary", "profile", "objective", "about", "overview", "professional profile"),
    "experience": (
        "experience", "work experience", "professional experience", "employment",
        "employment history", "work history", "career history", "relevant experience",
    ),
    "education": ("education", "academic background", "academics", "educational qualifications"),
    "skills": (
        "skills", "technical skills", "core skills", "technologies", "technical expertise",
        "core competencies", "skills & tools", "tech stack",
    ),
    "projects": ("projects", "key projects", "personal projects", "selected projects", "side projects", "portfolio"),
    "certifications": ("certifications", "certification", "certificates", "licenses", "licenses & certifications"),
    "awards": ("awards", "honors", "honours", "achievements", "awards & honors"),
    "publications": ("publications", "papers", "research"),
    "languages_spoken": ("languages spoken", "spoken languages"),
    "interests": ("interests", "hobbies", "volunteering", "activities", "references"),
    # Sidebar templates label the contact block. Recognising it keeps "CONTACT"
    # from being read as the candidate's name.
    "contact": ("contact", "contact details", "contact information", "personal details", "details"),
}

_PAGE_MARKER_RE = re.compile(r"^---\s*PAGE\s*\d+\s*---$", re.IGNORECASE)

# A date range such as "(2022 - Present)", "Jan 2020 – Mar 2022", or "2018-2022".
#
# The month prefix is an explicit alternation rather than `[A-Za-z]{3,9}`. That
# looser form swallowed the preceding word, so "University of Waterloo 2015 - 2019"
# parsed its start date as "Waterloo 2015" and left the institution as
# "University of".
_MONTH_NAMES = (
    "jan|january|feb|february|mar|march|apr|april|may|jun|june|jul|july|"
    "aug|august|sep|sept|september|oct|october|nov|november|dec|december"
)
_DATE_TOKEN = (
    r"(?:(?:" + _MONTH_NAMES + r")\.?\s+)?\d{4}(?:-\d{1,2})?"
    r"|Present|Current|Now|Ongoing"
)
DATE_RANGE_RE = re.compile(
    r"\(?\s*(?P<start>" + _DATE_TOKEN + r")\s*(?:-|to|until|through|–|—)\s*(?P<end>" + _DATE_TOKEN + r")\s*\)?",
    re.IGNORECASE,
)

TITLE_KEYWORDS = (
    "engineer", "developer", "manager", "director", "architect", "analyst", "scientist",
    "designer", "consultant", "intern", "specialist", "administrator", "lead", "head",
    "officer", "president", "founder", "associate", "coordinator", "researcher",
    "programmer", "technician", "supervisor", "principal", "staff", "cto", "ceo", "vp",
    "trainee", "apprentice", "freelance", "contractor", "strategist", "advisor",
    "owner", "partner", "executive", "editor", "writer", "recruiter", "accountant",
)

INSTITUTION_KEYWORDS = (
    "university", "college", "institute", "school", "academy", "polytechnic", "iit", "nit",
    "iiit", "iim", "universidad", "universite", "politecnico", "vishwavidyalaya",
)

# Suffixes that mark a fragment as an employer rather than a job title. Without
# these, "SDE Trainee - AI/ML (Backend & Data) | Antino Labs" picked the middle
# fragment as the employer purely because it came second.
#
# Restricted to unambiguous corporate and institutional suffixes: generic words
# like "ai", "tech" or "platform" also appear inside department names, and "ai"
# matched the fragment "AI/ML (Backend & Data)" and beat the real employer.
COMPANY_KEYWORDS = (
    "inc", "inc.", "llc", "ltd", "ltd.", "limited", "plc", "gmbh", "bv", "nv", "ag",
    "pvt", "corp", "corp.", "corporation", "company", "labs", "technologies",
    "solutions", "systems", "software", "group", "holdings", "ventures",
    "consulting", "industries", "networks", "foundation",
    "institute", "university", "college", "academy",
)

# Longer abbreviations come first so "BASc" is not clipped to "BA", and "BSc"
# not to "BS", which left the rest of the qualification in the field of study.
DEGREE_RE = re.compile(
    r"\b(B\.?A\.?Sc|B\.?Sc|M\.?Sc|B\.?Eng|M\.?Eng|B\.?\s?Tech|M\.?\s?Tech|"
    r"M\.?B\.?A\.?|MBA|Ph\.?\s?D\.?|"
    r"B\.?\s?S\.?|B\.?\s?A\.?|B\.?\s?E\.?|M\.?\s?S\.?|M\.?\s?A\.?|"
    r"Bachelors?(?:\s+of\s+\w+)?|Masters?(?:\s+of\s+\w+)?|"
    r"Doctorate|Associate(?:\s+of\s+\w+)?|Diploma)(?!\w)",
    re.IGNORECASE,
)

SEPARATOR_RE = re.compile(r"\s+(?:-|–|—|\||•|/{2})\s+|\s+\bat\b\s+", re.IGNORECASE)


def _looks_like_heading(text: str) -> bool:
    """Whether a line is typographically a section heading.

    Headings are short, carry no digits, and are set in capitals or title case.
    Without these guards, matching alias words anywhere would turn an ordinary
    bullet such as "Led 3 projects" into a section break.
    """
    if not text or len(text) > 60:
        return False
    if any(char.isdigit() for char in text):
        return False
    words = [word for word in text.split() if word]
    if not (1 <= len(words) <= 6):
        return False
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    if all(char.isupper() for char in letters):
        return True
    # Title case, ignoring the small joining words templates leave lowercase.
    joiners = {"and", "of", "in", "the", "&"}
    return all(word[0].isupper() or word.lower() in joiners for word in words if word[0].isalpha())


def _heading_key(line: str) -> Optional[str]:
    """Return the canonical section name if this line is a section heading.

    Compound headings are common ("EDUCATION & PUBLICATIONS", "AI PRODUCT
    PROJECTS"), so an alias is matched as a whole word anywhere in the heading,
    not just as the entire heading. When several aliases appear, the earliest one
    wins: "EDUCATION & PUBLICATIONS" is an education section that happens to also
    list papers, not a publications section.
    """
    text = clean_text(line).rstrip(":")
    if not _looks_like_heading(text):
        return None
    if text.endswith((".", ",", ";")):
        return None

    normalized = re.sub(r"[^a-z& ]+", " ", text.lower()).strip()
    normalized = re.sub(r"\s+", " ", normalized)
    if not normalized:
        return None

    best: Optional[Tuple[int, int, str]] = None  # (position, -length, key)
    for key, aliases in SECTION_ALIASES.items():
        for alias in aliases:
            for variant in {alias, alias.replace("&", "and")}:
                if normalized == variant:
                    return key
                match = re.search(rf"(?<!\w){re.escape(variant)}(?!\w)", normalized)
                if match:
                    candidate = (match.start(), -len(variant), key)
                    if best is None or candidate < best:
                        best = candidate
    return best[2] if best else None


def split_sections(resume_text: str) -> Dict[str, List[str]]:
    """Split raw resume text into `{section_name: [lines]}`.

    Text appearing before the first recognized heading is kept under `"_header"`,
    which is where the contact block almost always lives.
    """
    sections: Dict[str, List[str]] = {"_header": []}
    current = "_header"

    for raw_line in resume_text.splitlines():
        line = clean_text(raw_line)
        if not line or _PAGE_MARKER_RE.match(line):
            continue
        key = _heading_key(line)
        if key:
            current = key
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    return sections


# ==============================================================================
# CONTACT BLOCK
# ==============================================================================

_LOCATION_RE = re.compile(
    r"\b([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,2},\s*(?:[A-Z]{2}\b|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*))"
)
# "Based in Gurugram", "Location: Berlin" — a city with no region after it, which
# the comma-separated pattern above cannot see.
_LOCATION_PREFIX_RE = re.compile(
    r"\b(?:based\s+in|located\s+in|location)\s*:?\s*"
    r"([A-Z][A-Za-z.\-]+(?:\s+[A-Z][A-Za-z.\-]+){0,2})",
    re.IGNORECASE,
)

# Countries the country-inference step will name. A location's trailing component
# is only treated as a country when it appears here: "Based in Gurugram" yields a
# city, and recording "Gurugram" as the country of residence would produce wrong
# answers to every work-authorization question on an application form.
US_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)
CANADIAN_PROVINCES = frozenset("ON BC QC AB MB SK NS NB NL PE NT YT NU".split())

KNOWN_COUNTRIES: Dict[str, str] = {
    "usa": "United States", "us": "United States", "u.s.": "United States",
    "u.s.a.": "United States", "united states": "United States", "america": "United States",
    "uk": "United Kingdom", "united kingdom": "United Kingdom", "england": "United Kingdom",
    "scotland": "United Kingdom", "wales": "United Kingdom", "great britain": "United Kingdom",
    "india": "India", "canada": "Canada", "australia": "Australia", "germany": "Germany",
    "france": "France", "spain": "Spain", "italy": "Italy", "netherlands": "Netherlands",
    "ireland": "Ireland", "singapore": "Singapore", "japan": "Japan", "china": "China",
    "brazil": "Brazil", "mexico": "Mexico", "poland": "Poland", "portugal": "Portugal",
    "sweden": "Sweden", "norway": "Norway", "denmark": "Denmark", "finland": "Finland",
    "switzerland": "Switzerland", "austria": "Austria", "belgium": "Belgium",
    "new zealand": "New Zealand", "south africa": "South Africa", "uae": "United Arab Emirates",
    "united arab emirates": "United Arab Emirates", "israel": "Israel", "pakistan": "Pakistan",
    "bangladesh": "Bangladesh", "sri lanka": "Sri Lanka", "nigeria": "Nigeria", "kenya": "Kenya",
    "philippines": "Philippines", "indonesia": "Indonesia", "vietnam": "Vietnam",
    "south korea": "South Korea", "korea": "South Korea", "argentina": "Argentina",
    "chile": "Chile", "colombia": "Colombia", "romania": "Romania", "ukraine": "Ukraine",
}


def extract_contact(header_lines: List[str], full_text: str) -> Dict[str, Any]:
    """Extract name, email, phone, location, and profile links.

    Only the first page header is searched for the name, but the whole document is
    searched for email/phone/links, because some templates put them in a footer.
    """
    contact: Dict[str, Any] = {
        "full_name": None, "email": None, "phone": None, "location": None,
        "linkedin_url": None, "github_url": None, "portfolio_url": None,
    }

    email_match = EMAIL_RE.search(full_text)
    if email_match:
        contact["email"] = email_match.group(0)

    # Search the header first so a phone number in a bullet cannot win.
    for haystack in (" | ".join(header_lines), full_text):
        phone_match = PHONE_RE.search(haystack)
        if phone_match:
            normalized = normalize_phone(phone_match.group(0))
            if normalized:
                contact["phone"] = normalized
                break

    for match in re.finditer(r"(?:https?://)?(?:[\w.\-]+\.)?([\w\-]+)\.(\w{2,})(/[^\s,|)\]]*)?", full_text):
        url = normalize_url(match.group(0))
        if not url:
            continue
        host = match.group(1).lower()
        if host == "linkedin" and not contact["linkedin_url"] and match.group(3):
            contact["linkedin_url"] = url
        elif host == "github" and not contact["github_url"] and match.group(3):
            contact["github_url"] = url

    # Name: the first header line that reads like a person's name. Section
    # headings are skipped explicitly here rather than inside
    # `looks_like_person_name`, because only this module knows the heading list —
    # and many resumes legitimately set the candidate's name in capitals.
    for line in header_lines[:6]:
        if _heading_key(line):
            continue
        candidate = clean_text(line).split("|")[0].strip()
        if looks_like_person_name(candidate):
            contact["full_name"] = candidate
            break

    # Location: an explicit "City, ST" pattern in the header, or the word Remote.
    header_blob = " | ".join(header_lines[:12])
    location_match = _LOCATION_RE.search(header_blob)
    prefixed = _LOCATION_PREFIX_RE.search(header_blob)
    if location_match:
        contact["location"] = location_match.group(1).strip()
    elif prefixed:
        contact["location"] = prefixed.group(1).strip()
    elif re.search(r"\bremote\b", header_blob, re.IGNORECASE):
        contact["location"] = "Remote"

    return contact


# ==============================================================================
# EXPERIENCE
# ==============================================================================

def _looks_like_title(text: str) -> bool:
    """Whether a fragment reads like a job title rather than an employer name."""
    lowered = text.lower()
    return any(keyword in lowered for keyword in TITLE_KEYWORDS)


def _looks_like_company(text: str) -> bool:
    """Whether a fragment names an employer or institution.

    Matches on whole words so that "Bank of Baroda" counts while "Banking
    Operations Lead" does not.
    """
    words = re.findall(r"[a-z.]+", text.lower())
    return any(word.strip(".") in {k.strip(".") for k in COMPANY_KEYWORDS} for word in words)


def _split_company_and_title(text: str) -> Tuple[Optional[str], Optional[str]]:
    """Split a single-line experience header into (company, title)."""
    return _resolve_company_title([text])


def _resolve_company_title(fragments: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Work out (company, title) from one or more header fragments.

    Templates disagree on both the order and how many lines a role header spans,
    so the decision is made by which fragment carries a job-title keyword rather
    than by position. Fragments may be a single "Acme - Senior Engineer" line, or
    separate "Senior Engineer" and "Acme" lines from a stacked layout.
    """
    parts: List[str] = []
    for fragment in fragments:
        for part in SEPARATOR_RE.split(fragment or ""):
            cleaned = clean_text(part).strip(" ,-|–—")
            if cleaned:
                parts.append(cleaned)

    if not parts:
        return None, None
    if len(parts) == 1:
        single = parts[0]
        return (None, single) if _looks_like_title(single) else (single, None)

    titles = [part for part in parts if _looks_like_title(part)]
    others = [part for part in parts if not _looks_like_title(part)]

    if titles and others:
        # Among the non-title fragments, one naming a company or institution beats
        # one that is merely a department ("AI/ML (Backend & Data)").
        company = next((part for part in others if _looks_like_company(part)), others[0])
        return company, titles[0]
    if titles:
        # Every fragment reads as a title; the trailing one is usually the employer
        # ("Product Lead - Engineering | Helios Data").
        return parts[-1], parts[0]
    company = next((part for part in parts if _looks_like_company(part)), parts[0])
    other = next((part for part in parts if part != company), None)
    return company, other


def _continues_previous_bullet(
    line: str,
    current: Optional[Dict[str, Any]],
    pending: List[str],
) -> bool:
    """Whether this line is the tail of the bullet above it.

    Requires an unfinished previous bullet (no terminal punctuation) and a line
    that does not start a new thought — lowercase, or opening with a connective.
    Both conditions are needed: the first alone would swallow stacked role
    headers, the second alone would glue together genuinely separate bullets.
    """
    if current is None or pending or not current["description_bullets"]:
        return False
    previous = current["description_bullets"][-1]
    if previous.endswith((".", "!", "?", ":", ";")):
        return False
    text = clean_text(line)
    if not text or len(text) < 3:
        return False
    first = text.split()[0]
    return first[:1].islower() or first.lower() in {"and", "or", "with", "to", "for", "in", "the"}


def _is_header_fragment(line: str) -> bool:
    """Whether a line looks like part of a role header rather than a bullet.

    Header fragments are short and do not read as sentences. Prose that runs long
    or ends in a full stop is an achievement bullet, even when the template omits
    a bullet glyph.
    """
    text = clean_text(line)
    if not text or len(text) > 90:
        return False
    if text.endswith((".", ";")):
        return False
    return len(text.split()) <= 12


# A header ending in a lone year, e.g. "Product Analyst - Vizitor | SaaS, Remote 2025".
_TRAILING_YEAR_RE = re.compile(r"[\s,(–—-]*\b((?:19|20)\d{2})\b\)?\s*$")


def _extract_date_range(text: str) -> Tuple[Optional[str], Optional[str], str]:
    """Pull a date range out of a line, returning (start, end, remaining_text).

    A lone trailing year counts as a range covering that year. Plenty of resumes
    date short engagements with a single year, and requiring a start-end pair made
    those roles invisible — they were absorbed as bullets of the role above.
    """
    match = DATE_RANGE_RE.search(text)
    if match:
        start = normalize_date_string(match.group("start"))
        end_raw = match.group("end")
        end = "Present" if is_present_token(end_raw) else normalize_date_string(end_raw)
        remainder = (text[: match.start()] + " " + text[match.end():]).strip(" ,-|()–—")
        return start, end, clean_text(remainder)

    single = _TRAILING_YEAR_RE.search(text)
    if single:
        remainder = clean_text(text[: single.start()]).strip(" ,-|()–—")
        # Only when something is left to name the role; a bare year is not a header.
        if remainder:
            year = single.group(1)
            return year, year, remainder

    return None, None, text


def parse_experience(lines: List[str]) -> List[Dict[str, Any]]:
    """Parse the experience section into role entries with their bullets.

    Handles the three layouts resume templates actually produce:

    1. **Inline** - ``Acme Corp - Senior Engineer (2020 - Present)``
    2. **Right-aligned** - ``Acme Corp          Jan 2020 - Present`` with the job
       title on the following line.
    3. **Stacked** - the title, the employer, and the dates each on their own
       line, which Word and Google Docs templates favour.

    The date range is the anchor in every case. Short non-sentence lines are held
    as pending header fragments; a date line consumes them, and anything else
    flushes them into the current role's bullets so no content is silently lost.
    """
    entries: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    pending: List[str] = []

    def flush_pending() -> None:
        """Held lines turned out not to be a header, so treat them as bullets."""
        nonlocal pending
        if current is not None:
            for held in pending:
                if len(held) >= 10:
                    current["description_bullets"].append(held)
        pending = []

    index = 0
    while index < len(lines):
        line = lines[index]
        bullet_text = strip_bullet_prefix(line)
        is_bullet = bullet_text != line  # The line began with a bullet glyph.
        start, end, remainder = _extract_date_range(line)

        # A long sentence that merely mentions a date range ("Ran the 2021 - 2022
        # migration...") is a bullet, not a new employer.
        if start and not is_bullet and len(remainder) <= 120:
            # The header is whatever sits beside the dates, or — when the dates
            # occupy the line alone — the lines immediately above them.
            fragments = [remainder] if remainder else pending[-2:]
            company, title = _resolve_company_title(fragments)
            pending = []

            if not company and not title:
                index += 1
                continue

            # Right-aligned layouts put the title on the next line.
            if company and not title and index + 1 < len(lines):
                nxt = lines[index + 1]
                if (
                    strip_bullet_prefix(nxt) == nxt
                    and _is_header_fragment(nxt)
                    and not DATE_RANGE_RE.search(nxt)
                    and _looks_like_title(nxt)
                ):
                    title = clean_text(nxt)
                    index += 1

            current = {
                "company": company or title or "",
                "title": title or company or "",
                "location": None,
                "start_date": start,
                "end_date": end,
                "is_current": end is None or is_present_token(end or ""),
                "description_bullets": [],
                "locked_facts": [],
            }
            entries.append(current)
            index += 1
            continue

        if is_bullet:
            flush_pending()
            if len(bullet_text) >= 10 and current is not None:
                current["description_bullets"].append(bullet_text)
        elif _continues_previous_bullet(line, current, pending):
            # A bullet wrapped onto the next line. PDF extraction gives one line
            # per visual row, so a long achievement arrives in pieces; joining
            # them keeps the sentence — and any metric split across the break —
            # intact.
            current["description_bullets"][-1] = clean_text(
                current["description_bullets"][-1] + " " + clean_text(line)
            )
        elif _is_header_fragment(line):
            # Could be the start of a stacked header; decide when a date arrives.
            pending.append(clean_text(line))
            if len(pending) > 3:
                # Too far from any date to be a header for one.
                stale, pending = pending[:-3], pending[-3:]
                if current is not None:
                    current["description_bullets"].extend(s for s in stale if len(s) >= 10)
        else:
            flush_pending()
            if current is not None and len(bullet_text) >= 10:
                current["description_bullets"].append(bullet_text)

        index += 1

    flush_pending()

    # Drop malformed rows rather than emitting a role with a blank employer.
    return [entry for entry in entries if entry["company"] and entry["title"]]


# ==============================================================================
# EDUCATION
# ==============================================================================

def _parse_degree_fragment(text: str) -> Tuple[Optional[str], str]:
    """Pull (degree, field_of_study) out of a fragment, if it names a degree."""
    match = DEGREE_RE.search(text)
    if not match:
        return None, ""
    degree = clean_text(match.group(0))
    tail = text[match.end():]
    field_match = re.match(r"\s*(?:in|of)\s+(.+)", tail, re.IGNORECASE)
    field = clean_text(field_match.group(1) if field_match else tail).strip(" ,-|–—")
    return degree, field


def parse_education(lines: List[str]) -> List[Dict[str, Any]]:
    """Parse the education section into degree entries.

    Like experience, an entry may span several lines: the degree frequently sits
    on its own line above or below the institution, and the dates on a third.
    The institution anchors the entry; the degree is then taken from the same
    line, the line before, or the line after, whichever names one.
    """
    entries: List[Dict[str, Any]] = []
    previous: Optional[str] = None

    for index, line in enumerate(lines):
        start, end, remainder = _extract_date_range(line)
        has_institution = any(keyword in remainder.lower() for keyword in INSTITUTION_KEYWORDS)

        parts = [clean_text(part).strip(" ,-|–—") for part in SEPARATOR_RE.split(remainder)]
        parts = [part for part in parts if part]
        degree_here, field_here = _parse_degree_fragment(remainder)

        if not has_institution:
            # A line naming a degree *and* something else ("B.Tech, CS | CSVTU,
            # Bhilai") carries its own institution under an unrecognised name, so
            # it falls through to be treated as a full entry.
            carries_institution = bool(degree_here) and len(parts) >= 2
            if not carries_institution:
                if degree_here and entries and entries[-1]["degree"] == "Degree":
                    # A degree on its own line completes the entry above it.
                    entries[-1]["degree"] = degree_here
                    if field_here:
                        entries[-1]["field_of_study"] = field_here
                elif entries and start and not entries[-1]["start_date"] and not remainder:
                    # A bare date line belonging to the entry above it.
                    entries[-1]["start_date"], entries[-1]["end_date"] = start, end
                # Remembered so the next line, if it names an institution, can
                # look back here for its degree.
                previous = remainder or previous
                continue

        institution = next(
            (part for part in parts if any(k in part.lower() for k in INSTITUTION_KEYWORDS)),
            "",
        )
        if not institution and degree_here:
            # The school name carries no recognizable keyword ("CSVTU, Bhilai"),
            # so take the fragment that is not the degree. Requiring a keyword
            # dropped these qualifications entirely.
            institution = next((part for part in parts if not DEGREE_RE.search(part)), "")
        if len(institution) < 2:
            continue

        # Degree: this line first, then the line above, then the line below.
        degree, field = "", ""
        for candidate in parts:
            if candidate == institution:
                continue
            degree, field = _parse_degree_fragment(candidate)
            if degree:
                break
        if not degree and previous:
            degree, field = _parse_degree_fragment(previous)
        if not degree and index + 1 < len(lines):
            nxt = clean_text(lines[index + 1])
            if not any(k in nxt.lower() for k in INSTITUTION_KEYWORDS):
                degree, field = _parse_degree_fragment(nxt)

        gpa_match = re.search(r"\bGPA[:\s]*([0-9.]+(?:\s*/\s*[0-9.]+)?)", remainder, re.IGNORECASE)

        entries.append({
            "institution": institution,
            "degree": degree or "Degree",
            "field_of_study": field or "Not specified",
            "start_date": start,
            "end_date": end,
            "gpa": clean_text(gpa_match.group(1)) if gpa_match else None,
            "honors": [],
        })
        previous = remainder

    return entries


# ==============================================================================
# SKILLS
# ==============================================================================

CATEGORY_HINTS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("languages", ("language", "programming")),
    ("frameworks", ("framework", "librar", "front-end", "frontend", "back-end", "backend", "web")),
    ("cloud_devops", ("cloud", "devops", "infrastructure", "platform", "ci/cd", "orchestration", "deployment")),
    ("developer_tools", ("tool", "database", "databases", "storage", "messaging", "software", "ide", "testing")),
    ("domain_knowledge", ("architecture", "domain", "concept", "competenc", "methodolog", "practice", "specialt")),
)

# Fallback classifier for resumes that list skills without category labels.
KNOWN_TECH: Dict[str, str] = {
    **{key: "languages" for key in (
        "python", "go", "golang", "rust", "java", "javascript", "typescript", "c", "c++", "c#",
        "ruby", "php", "scala", "kotlin", "swift", "sql", "bash", "shell", "r", "matlab", "perl",
        "elixir", "haskell", "dart", "objective-c", "groovy", "lua",
    )},
    **{key: "frameworks" for key in (
        "react", "angular", "vue", "svelte", "next.js", "nuxt", "django", "flask", "fastapi",
        "spring", "spring boot", "express", "node.js", "nodejs", "rails", "laravel", ".net",
        "pytorch", "tensorflow", "keras", "scikit-learn", "pandas", "numpy", "grpc", "graphql",
        "tailwind", "bootstrap", "jquery", "qt",
    )},
    **{key: "cloud_devops" for key in (
        "aws", "gcp", "google cloud", "azure", "kubernetes", "k8s", "docker", "terraform",
        "ansible", "helm", "jenkins", "github actions", "gitlab ci", "circleci", "argocd",
        "prometheus", "grafana", "datadog", "cloudformation", "pulumi", "openshift", "nginx",
        "serverless", "lambda", "ec2", "s3",
    )},
    **{key: "developer_tools" for key in (
        "git", "github", "gitlab", "jira", "postgresql", "postgres", "mysql", "mongodb",
        "redis", "kafka", "rabbitmq", "elasticsearch", "cassandra", "dynamodb", "cockroachdb",
        "sqlite", "snowflake", "spark", "hadoop", "airflow", "kibana", "splunk", "linux",
        "vim", "vs code", "pytest", "junit", "selenium", "figma",
    )},
}


def _classify_skill(skill: str) -> str:
    """Bucket a single skill using a known-technology table, defaulting to domain knowledge."""
    return KNOWN_TECH.get(skill.strip().lower(), "domain_knowledge")


def _join_wrapped_skill_lines(lines: List[str]) -> List[str]:
    """Rejoin skill lines that PDF extraction split across visual rows.

    A long "Product: ..., stakeholder / management, Jira, Figma." wraps mid-list,
    and treating the tail as its own line produced "stakeholder" and "management"
    as two separate skills.
    """
    joined: List[str] = []
    for line in lines:
        text = clean_text(line)
        if not text:
            continue
        starts_entry = strip_bullet_prefix(text) != text or re.match(r"^[^:]{1,40}:", text)
        # Only a line that visibly continues the one above is joined: the previous
        # line broke mid-list, or this one starts mid-sentence. A sidebar that
        # lists one capitalised skill per line must stay as separate skills.
        continues = bool(joined) and not starts_entry and (
            joined[-1].endswith(",") or text[:1].islower()
        )
        if continues:
            joined[-1] = clean_text(joined[-1] + " " + text)
        else:
            joined.append(text)
    return joined


def parse_skills(lines: List[str]) -> Dict[str, List[str]]:
    """Parse the skills section into the five schema categories.

    Labelled lines ("Languages: Python, Go") are routed by their label; unlabelled
    entries fall back to a per-skill lookup table.
    """
    skills: Dict[str, List[str]] = {
        "languages": [], "frameworks": [], "developer_tools": [],
        "cloud_devops": [], "domain_knowledge": [],
    }

    for line in _join_wrapped_skill_lines(lines):
        text = strip_bullet_prefix(line)
        if not text:
            continue

        label, _, remainder = text.partition(":")
        if remainder and len(label) <= 60:
            bucket = None
            label_lower = label.lower()
            for category, hints in CATEGORY_HINTS:
                if any(hint in label_lower for hint in hints):
                    bucket = category
                    break
            items = [clean_text(item) for item in re.split(r"[,;|]", remainder)]
            items = [item for item in items if item]
            if bucket:
                skills[bucket].extend(items)
            else:
                for item in items:
                    skills[_classify_skill(item)].append(item)
        else:
            for item in (clean_text(part) for part in re.split(r"[,;|]", text)):
                if item and len(item) <= 60:
                    skills[_classify_skill(item)].append(item)

    return {
        category: dedupe_preserving_order(value.strip(" .;") for value in values)
        for category, values in skills.items()
    }


# ==============================================================================
# PROJECTS AND CERTIFICATIONS
# ==============================================================================

def parse_projects(lines: List[str]) -> List[Dict[str, Any]]:
    """Parse the projects section into project entries.

    A non-bullet line opens a project; the bullets beneath it become its
    description. A project with no description text at all is dropped.
    """
    entries: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None

    for line in lines:
        bullet_text = strip_bullet_prefix(line)
        is_bullet = bullet_text != line

        if not is_bullet and len(bullet_text) <= 120:
            _, _, remainder = _extract_date_range(bullet_text)
            technologies: List[str] = []
            tech_match = re.search(r"[(\[]([^)\]]+)[)\]]\s*$", remainder)
            if tech_match:
                technologies = [clean_text(item) for item in re.split(r"[,;|]", tech_match.group(1))]
                technologies = [item for item in technologies if item and len(item) <= 40]
                remainder = clean_text(remainder[: tech_match.start()])

            parts = [clean_text(part).strip(" ,-|") for part in SEPARATOR_RE.split(remainder)]
            parts = [part for part in parts if part]
            title = parts[0] if parts else remainder
            if not title:
                continue
            current = {
                "title": title,
                "role": None,
                "technologies": technologies,
                "description": "",
                "link": normalize_url(remainder) if "http" in remainder or ".com" in remainder else None,
                "locked_facts": [],
            }
            entries.append(current)
        elif current is not None and bullet_text:
            current["description"] = clean_text(f"{current['description']} {bullet_text}").strip()

    return [entry for entry in entries if len(entry["description"]) >= 3]


def parse_certifications(lines: List[str]) -> List[Dict[str, Any]]:
    """Parse the certifications section.

    An entry is kept only when both a certification name and an issuer are present;
    inventing an issuer would create a verifiable claim the candidate cannot back up.
    """
    entries: List[Dict[str, Any]] = []
    for line in lines:
        text = strip_bullet_prefix(line)
        if len(text) < 4:
            continue
        _, _, remainder = _extract_date_range(text)
        date_match = DATE_RANGE_RE.search(text)
        issue_date = normalize_date_string(date_match.group("start")) if date_match else None
        if not issue_date:
            # A single year rather than a range, e.g. "... — Amazon (2022)".
            year_match = re.search(r"\(?\b((?:19|20)\d{2})\b\)?\s*$", remainder)
            if year_match:
                issue_date = year_match.group(1)
                remainder = clean_text(remainder[: year_match.start()]).strip(" ,-|")
            else:
                loose_year = re.search(r"\b(19|20)\d{2}\b", text)
                issue_date = loose_year.group(0) if loose_year else None

        parts = [clean_text(part).strip(" ,-|") for part in SEPARATOR_RE.split(remainder)]
        parts = [part for part in parts if part]
        if len(parts) < 2:
            parts = [clean_text(part).strip() for part in remainder.split(",") if clean_text(part).strip()]
        if len(parts) < 2:
            continue

        # The issuing body is the trailing segment; everything before it is the
        # certification's name. Taking parts[0] as the name truncated qualified
        # titles such as "AWS Certified Developer - Associate - Amazon Web
        # Services", which lost its level and named "Associate" as the issuer.
        entries.append({
            "name": " - ".join(parts[:-1]),
            "issuer": parts[-1],
            "issue_date": issue_date,
            "credential_url": normalize_url(text) if "http" in text else None,
        })
    return entries


# ==============================================================================
# TOP-LEVEL EXTRACTION
# ==============================================================================

REQUIRED_CONTACT_FIELDS = ("full_name", "email")


def build_profile_dict(resume_text: str, *, source_document: Optional[str] = None) -> Dict[str, Any]:
    """Extract a `CandidateProfile`-shaped dict from resume text without inventing facts.

    Raises:
        ResumeParseError: when the candidate's name or email cannot be located.
            These two cannot be derived from anything else, and guessing them would
            put the wrong person's details on an application.
    """
    sections = split_sections(resume_text)
    # A sidebar template labels its contact block, so those lines are part of the
    # header for contact purposes even though a heading separates them.
    header_lines = sections.get("_header", []) + sections.get("contact", [])

    contact = extract_contact(header_lines, resume_text)
    missing = [field for field in REQUIRED_CONTACT_FIELDS if not contact.get(field)]
    if missing:
        raise ResumeParseError(
            missing,
            "Add the missing detail to the resume, or configure an LLM API key in .env "
            "so the richer extractor can run.",
        )

    experience = parse_experience(sections.get("experience", []))
    education = parse_education(sections.get("education", []))
    skills = parse_skills(sections.get("skills", []))
    projects = parse_projects(sections.get("projects", []))
    certifications = parse_certifications(sections.get("certifications", []))

    # Years of experience come from the role dates, with overlapping roles merged.
    intervals = []
    for entry in experience:
        start = parse_partial_date(entry.get("start_date"))
        if start is None:
            continue
        from datetime import date as _date

        end = _date.today() if entry.get("is_current") else parse_partial_date(entry.get("end_date"))
        intervals.append((start, max(start, end or _date.today())))
    years_of_experience = years_between(intervals) if intervals else 0.0

    summary = _build_summary(sections.get("summary", []), experience, skills, years_of_experience)

    return {
        "contact": contact,
        "summary": summary,
        "work_authorization": _infer_work_authorization(resume_text, contact.get("location")),
        "education": education,
        "experience": experience,
        "skills": skills,
        "projects": projects,
        "certifications": certifications,
        "years_of_experience": years_of_experience,
        "source_document": source_document,
        "extraction_method": "deterministic",
    }


def _build_summary(
    summary_lines: List[str],
    experience: List[Dict[str, Any]],
    skills: Dict[str, List[str]],
    years_of_experience: float,
) -> str:
    """Use the resume's own summary, or assemble one strictly from extracted facts.

    The assembled fallback restates the most recent title, the computed tenure, and
    skills that appear in the document. It adds no claim that is not already there.
    """
    written = clean_text(" ".join(strip_bullet_prefix(line) for line in summary_lines))
    if len(written) >= 20:
        return written

    if experience:
        role = experience[0].get("title") or "Professional"
        headline = f"{role} with {years_of_experience:g} years of professional experience."
    else:
        headline = "Candidate profile extracted from submitted resume."

    top_skills = (skills.get("languages", []) + skills.get("cloud_devops", []) + skills.get("frameworks", []))[:6]
    if top_skills:
        headline += " Core skills: " + ", ".join(top_skills) + "."
    # The schema requires at least 10 characters; the headline always exceeds that.
    return headline


_SPONSORSHIP_RE = re.compile(
    r"(require[sd]?\s+sponsorship|need\s+sponsorship|visa\s+sponsorship\s+required)", re.IGNORECASE
)
_NO_SPONSORSHIP_RE = re.compile(
    r"(no\s+sponsorship\s+required|without\s+sponsorship|authorized\s+to\s+work|work\s+authorization|"
    r"citizen|permanent\s+resident|green\s+card)",
    re.IGNORECASE,
)
_VISA_RE = re.compile(r"\b(H-?1B|L-?1|F-?1|OPT|CPT|TN visa|EAD|Green Card|Blue Card|Tier 2)\b", re.IGNORECASE)


def _infer_work_authorization(resume_text: str, location: Optional[str]) -> Dict[str, Any]:
    """Read work authorization from explicit statements only.

    Missing sponsorship and authorization information stays unknown. Residence
    is recorded separately and never used as proof of eligibility.
    """
    visa_match = _VISA_RE.search(resume_text)
    requires_sponsorship = (False if _NO_SPONSORSHIP_RE.search(resume_text)
                            else True if _SPONSORSHIP_RE.search(resume_text) else None)

    country = None
    if location:
        # A two-letter region code implies its country, but only when it really is
        # one: "Toronto, ON" is Ontario, and treating every pair of capitals as a
        # US state put Canadian candidates in the wrong country.
        tail = location.split(",")[-1].strip()
        if tail.upper() in CANADIAN_PROVINCES:
            country = "Canada"
        elif tail.upper() in US_STATE_CODES:
            country = "United States"
        else:
            # Only a recognised country name counts. A bare city ("Gurugram")
            # would otherwise be recorded as the country of residence and drive
            # wrong answers to work-authorization questions.
            country = KNOWN_COUNTRIES.get(tail.casefold())
            if country is None:
                for part in (piece.strip() for piece in location.split(",")):
                    country = KNOWN_COUNTRIES.get(part.casefold())
                    if country:
                        break

    return {
        "citizenship": [],
        "current_country": country or "Unspecified",
        "authorized_countries": [],
        "requires_sponsorship": requires_sponsorship,
        "visa_status": clean_text(visa_match.group(0)) if visa_match else None,
    }
