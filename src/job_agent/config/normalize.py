"""Shared normalization, parsing, and validation helpers.

Used by the Pydantic schemas (`config.schema`), the resume intake parsers, and the
sourcing normalizers so that every stage of the pipeline agrees on what a clean
date, URL, phone number, or skill list looks like.

Every helper here is deterministic and side-effect free: given the same input it
always produces the same output, which is what allows the cryptographic fact seal
in `CandidateProfile` to stay stable across runs.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Iterable, List, Optional, Sequence, Tuple

# ==============================================================================
# TEXT
# ==============================================================================

# Unicode bullets, dashes, and quotes that survive PDF extraction and corrupt
# downstream string comparisons (and Typst compilation).
# Any short run of leading symbols is a list marker, but never "$340k saved" or "(2020)".
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:[^\w\s$\"'(\[]{1,3}|\d+[.)])\s+")
_WHITESPACE_RE = re.compile(r"[ \t   ]+")
# PDF generators encode list bullets with symbol-font code points (DEL, or the
# U+F0xx private-use block); those carry the "this line is a bullet" signal.
_BULLET_GLYPH_RE = re.compile(r"[-]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x80-\x9f]")

# pdfplumber writes "(cid:127)" when a font maps a glyph to a code it cannot
# resolve to a character. In resumes these are nearly always list bullets from a
# symbol font, and leaving them in place puts literal "(cid:127)" text into the
# profile, the compiled PDF, and the outreach emails.
_CID_RE = re.compile(r"\(cid:(\d+)\)")
# Codes that are a bullet in the symbol fonts resume templates actually use.
_CID_BULLET_CODES = {127, 149, 183, 8226, 61623, 61607, 61553}
_NEWLINES_RE = re.compile(r"\n{3,}")

_LIGATURES = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl",
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
}


def _resolve_cid(match: "re.Match[str]") -> str:
    """Turn an unresolved "(cid:NNN)" glyph into a bullet, or drop it.

    A code that is a bullet in the common symbol fonts becomes one. Anything else
    is removed rather than guessed at, because inventing a character would corrupt
    a word, while dropping one leaves the surrounding text readable.
    """
    code = int(match.group(1))
    return "•" if code in _CID_BULLET_CODES else ""


def clean_text(value: Optional[str]) -> str:
    """Normalize unicode, collapse runs of whitespace, and strip the result.

    PDF extraction routinely yields ligatures, non-breaking spaces, and smart
    quotes; leaving them in place breaks exact-match metric verification later.
    """
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value))
    for bad, good in _LIGATURES.items():
        text = text.replace(bad, good)
    text = _CID_RE.sub(_resolve_cid, text)
    # Preserve symbol-font bullets as a real bullet character, then drop every
    # other control character rather than letting it reach a PDF or a form field.
    text = _BULLET_GLYPH_RE.sub("•", text)
    text = _CONTROL_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    return text.strip()


def strip_bullet_prefix(value: str) -> str:
    """Remove a leading bullet glyph or numbered-list marker from a line."""
    return _BULLET_PREFIX_RE.sub("", value or "").strip()


def dedupe_preserving_order(items: Iterable[str], *, casefold: bool = True) -> List[str]:
    """Drop duplicates while preserving first-seen order and original casing.

    Casing is preserved because skill names ("AWS", "gRPC") are user-visible in the
    compiled resume, but comparison is case-insensitive so "aws" and "AWS" collapse.
    """
    seen: set[str] = set()
    result: List[str] = []
    for raw in items:
        cleaned = clean_text(raw)
        if not cleaned:
            continue
        key = cleaned.casefold() if casefold else cleaned
        if key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def truncate(value: str, limit: int) -> str:
    """Hard-cap a string's length, appending an explicit truncation marker."""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 15)].rstrip() + " ...[truncated]"


# ==============================================================================
# DATES
# ==============================================================================

PRESENT_TOKENS = {"present", "current", "now", "ongoing", "to date", "till date", "date"}

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}

_ISO_RE = re.compile(r"^(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?")
_MONTH_YEAR_RE = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$")
_SLASH_RE = re.compile(r"^(\d{1,2})/(\d{4})$")

# Resumes should not contain dates outside this window; anything else is a parse error.
MIN_YEAR = 1940
MAX_YEAR = date.today().year + 10


def is_present_token(value: Optional[str]) -> bool:
    """Whether a date string means 'still ongoing' rather than a calendar date."""
    return bool(value) and clean_text(value).strip(" .").casefold() in PRESENT_TOKENS


def parse_partial_date(value: Optional[str]) -> Optional[date]:
    """Parse 'YYYY', 'YYYY-MM', 'YYYY-MM-DD', 'Mar 2021', or '03/2021' into a date.

    Missing month/day components default to January 1st so that intervals remain
    comparable. Returns None for 'Present' and for anything unparseable.
    """
    if not value:
        return None
    text = clean_text(value).strip(" .,")
    if not text or is_present_token(text):
        return None

    year = month = day = None

    iso = _ISO_RE.match(text)
    if iso:
        year = int(iso.group(1))
        month = int(iso.group(2)) if iso.group(2) else 1
        day = int(iso.group(3)) if iso.group(3) else 1
    else:
        month_year = _MONTH_YEAR_RE.match(text)
        if month_year:
            month_name = month_year.group(1).casefold()
            if month_name not in _MONTHS:
                return None
            month, year, day = _MONTHS[month_name], int(month_year.group(2)), 1
        else:
            slash = _SLASH_RE.match(text)
            if slash:
                month, year, day = int(slash.group(1)), int(slash.group(2)), 1
            else:
                return None

    if not (MIN_YEAR <= year <= MAX_YEAR) or not (1 <= month <= 12):
        return None
    try:
        return date(year, month, min(day, 28))
    except ValueError:
        return None


def normalize_date_string(value: Optional[str]) -> Optional[str]:
    """Canonicalize a resume date to 'YYYY-MM', 'YYYY', or 'Present'.

    Unparseable input is returned cleaned but unchanged rather than discarded, so
    that a badly formatted date never silently erases a real role.
    """
    if value is None:
        return None
    text = clean_text(value)
    if not text:
        return None
    if is_present_token(text):
        return "Present"

    parsed = parse_partial_date(text)
    if parsed is None:
        return text
    # Preserve 'year only' precision when that is all the source gave us.
    if re.fullmatch(r"\d{4}", text):
        return f"{parsed.year:04d}"
    return f"{parsed.year:04d}-{parsed.month:02d}"


def merge_date_intervals(
    intervals: Sequence[Tuple[date, date]],
) -> List[Tuple[date, date]]:
    """Merge overlapping [start, end] spans so concurrent roles are not double counted."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda pair: pair[0])
    merged: List[Tuple[date, date]] = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def years_between(intervals: Sequence[Tuple[date, date]]) -> float:
    """Total non-overlapping years covered by the given date intervals."""
    total_days = sum((end - start).days for start, end in merge_date_intervals(intervals))
    return round(max(0.0, total_days) / 365.25, 1)


def utc_now_iso() -> str:
    """Current UTC timestamp in ISO-8601 form."""
    return datetime.now(timezone.utc).isoformat()


def parse_posting_timestamp(value: Optional[str]) -> Optional[datetime]:
    """Best-effort parse of a job board / ATS posting timestamp into aware UTC."""
    if not value:
        return None
    text = clean_text(value)
    if not text:
        return None
    # Numeric epoch (Lever and Ashby both emit milliseconds).
    if re.fullmatch(r"\d{10}", text):
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    if re.fullmatch(r"\d{13}", text):
        return datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed_date = parse_partial_date(text)
        if parsed_date is None:
            return None
        return datetime(parsed_date.year, parsed_date.month, parsed_date.day, tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# ==============================================================================
# CONTACT DETAILS
# ==============================================================================

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# A phone number must carry one of three positive signals: an international
# prefix, a parenthesised area code, or three or more digit groups. Requiring one
# of them is what stops a year range ("2020 2024") or a metric ("99.999") from
# being read as a phone number, while still accepting the many national formats —
# "+91 98450 11234" groups as 5+5, which a fixed 3-3-4 pattern rejects outright.
PHONE_RE = re.compile(
    r"(?:"
    r"\+\d{1,3}[\s.\-]?\(?\d{2,5}\)?(?:[\s.\-]?\d{2,5}){1,3}"      # +country ...
    r"|\(\d{2,5}\)[\s.\-]?\d{2,5}(?:[\s.\-]?\d{2,5}){0,2}"          # (area) ...
    r"|\d{3,5}[\s.\-]\d{2,5}[\s.\-]\d{2,5}(?:[\s.\-]\d{2,5})?"      # three or more groups
    r")"
)
URL_RE = re.compile(r"(?:https?://)?(?:www\.)?[A-Za-z0-9\-]+\.[A-Za-z]{2,}(?:/[^\s,|]*)?")


def normalize_url(value: Optional[str], *, require_host: bool = True) -> Optional[str]:
    """Add a scheme to a bare URL and reject anything that is not host-shaped.

    Returns None rather than raising so that a malformed LinkedIn URL degrades to
    'absent' instead of failing the entire profile.
    """
    if not value:
        return None
    text = clean_text(value).strip("<>()[],;")
    if not text:
        return None
    if text.casefold() in {"n/a", "na", "none", "null", "-"}:
        return None
    if not text.lower().startswith(("http://", "https://")):
        text = "https://" + text.lstrip("/")

    match = re.match(r"^https?://([^/\s]+)(/.*)?$", text)
    if not match:
        return None
    host = match.group(1)
    if require_host and ("." not in host or host.startswith(".") or host.endswith(".")):
        return None
    return text


def normalize_phone(value: Optional[str]) -> Optional[str]:
    """Keep a phone number only if it carries a plausible number of digits.

    Anything with fewer than 7 or more than 15 digits (E.164's ceiling) is treated
    as a misparse and dropped, so no invented number reaches an application form.
    """
    if not value:
        return None
    text = clean_text(value)
    digits = re.sub(r"\D", "", text)
    if not (7 <= len(digits) <= 15):
        return None
    return text


# Words that head a document rather than name a person.
_DOCUMENT_TITLES = {"curriculum vitae", "resume", "cv", "curriculum", "profile", "bio", "biodata"}


def looks_like_person_name(value: str) -> bool:
    """Whether a line plausibly holds the candidate's name.

    All-caps is accepted: a great many resumes set the name in capitals, and
    rejecting that shape made the parser skip the real name and take the headline
    beneath it ("Associate Product Manager") as the candidate's name instead.
    Section headings are excluded by the caller, which knows the heading list;
    only generic document titles are filtered here.
    """
    text = clean_text(value)
    if not (2 <= len(text) <= 80):
        return False
    if EMAIL_RE.search(text) or "://" in text or "@" in text:
        return False
    if any(char.isdigit() for char in text):
        return False
    if text.casefold().strip(" .:-") in _DOCUMENT_TITLES:
        return False
    words = text.split()
    if not (1 <= len(words) <= 5):
        return False
    return all(re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*", word) for word in words)


def tokenize(value: str, *, min_length: int = 3) -> set:
    """Lowercase alphanumeric tokens, used for keyword overlap scoring."""
    pattern = r"[a-z0-9+#.]{%d,}" % min_length
    return set(re.findall(pattern, clean_text(value).lower()))


def strip_html(value: Optional[str]) -> str:
    """Convert an HTML job description into readable plain text.

    Block-level tags become newlines and list items become bullets so that the
    structure a re-ranker relies on (requirement lists) survives the conversion.
    """
    if not value:
        return ""
    text = str(value)
    text = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|div|ul|ol|h[1-6]|tr|section)>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    # Unescape the entities that actually show up in ATS payloads.
    replacements = (
        ("&nbsp;", " "), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&quot;", '"'), ("&#39;", "'"), ("&rsquo;", "'"), ("&mdash;", "-"),
    )
    for entity, char in replacements:
        text = text.replace(entity, char)
    text = re.sub(r"&#x?[0-9a-fA-F]+;", " ", text)
    lines = [clean_text(line) for line in text.splitlines()]
    joined = "\n".join(line for line in lines if line)
    return _NEWLINES_RE.sub("\n\n", joined).strip()
