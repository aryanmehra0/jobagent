"""Extract and classify email addresses from job posts and web pages."""

from __future__ import annotations

import html
import re
from typing import Dict, List, Optional, Tuple

# Plain addresses. The lookarounds stop a match from starting mid-word or
# swallowing a trailing full stop ("mail hr@acme.com.").
_EMAIL_RE = re.compile(
    r"(?<![\w.+-])([A-Za-z0-9](?:[A-Za-z0-9._%+-]{0,62}[A-Za-z0-9])?)"
    r"@((?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,24})(?![\w-])"
)

# Obfuscated forms people use to dodge scrapers: "hr [at] acme [dot] com",
# "hr(at)acme(dot)com", "hr {at} acme {dot} co {dot} in". The brackets are
# required: bare "hr at acme dot com" is too easily ordinary prose.
_AT = r"\s*[\[\(\{<]\s*at\s*[\]\)\}>]\s*"
_DOT = r"\s*[\[\(\{<]\s*dot\s*[\]\)\}>]\s*"
_OBFUSCATED_RE = re.compile(
    rf"(?<![\w.])([A-Za-z0-9][A-Za-z0-9._%+-]*){_AT}([A-Za-z0-9-]+(?:{_DOT}[A-Za-z0-9-]+)+)",
    re.IGNORECASE,
)

# Domains that appear in markup, templates and tooling, never as a real contact.
_JUNK_DOMAINS = {
    "example.com", "example.org", "example.net", "domain.com", "email.com", "yourcompany.com",
    "company.com", "yourdomain.com", "test.com", "sentry.io", "sentry-next.wixpress.com",
    "wixpress.com", "wix.com", "godaddy.com", "squarespace.com", "mailchimp.com",
    "schema.org", "w3.org", "googleusercontent.com", "cloudflare.com", "localhost",
}
_JUNK_LOCAL_PARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster",
    "abuse", "webmaster", "hostmaster", "unsubscribe", "bounce", "bounces", "user", "username",
    "name", "email", "your.name", "yourname", "firstname.lastname", "john.doe", "jane.doe",
}
# Mailboxes a company publishes for something other than hiring. A resume sent
# to accommodations@ or privacy@ goes to the wrong team and reflects badly.
_NOT_FOR_RESUMES = {
    "accommodation", "accommodations", "accessibility", "privacy", "dataprotection", "dpo", "gdpr", "legal",
    "compliance", "press", "media", "pr", "investor", "investors", "ir", "security", "sales", "billing",
    "accounts", "invoice", "invoices", "payments", "orders", "partners", "partnerships", "marketing",
    "grievance", "grievances", "whistleblower", "ethics", "fraud", "phishing", "vendor", "vendors", "procurement",
}
_NEVER_FOR_RESUMES = (
    "fraud", "disabilit", "accommodat", "accomodat", "acommodat", "accessib", "helpdesk", "alumni", "privacy", "ethic", "complian", "grievance",
    "whistle", "investor", "security", "abuse", "phish", "scam", "unsubscribe", "noreply", "no-reply",
    "donotreply", "legal", "dataprotection", "gdpr", "media", "press", "newsletter", "feedback", "complaint",
)
# Retina asset names such as "logo@2x.png" match an address pattern.
_ASSET_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".css", ".js", ".woff", ".woff2")

_HIRING_TOKENS = (
    "hr", "careers", "career", "jobs", "job", "recruit", "recruiting", "recruitment", "recruiter",
    "talent", "hiring", "hire", "resume", "resumes", "cv", "apply", "applications", "people",
    "join", "joinus", "internship", "internships", "placement", "placements", "ta", "humanresources",
)
_HIRING_SUBSTRINGS = ("recruit", "talent", "career", "hiring", "resume", "jobs", "joinus", "joinour", "humanresource")
_GENERAL_TOKENS = (
    "info", "contact", "hello", "hi", "support", "admin", "team", "office", "enquiry", "enquiries",
    "inquiry", "inquiries", "mail", "general", "help", "connect",
)

# Order of usefulness for sending a resume.
KIND_RANK: Dict[str, int] = {"hiring": 0, "person": 1, "general": 2, "other": 3}


def _local_tokens(local: str) -> List[str]:
    return [token for token in re.split(r"[._+\-0-9]+", local.lower()) if token]


def classify_email(email: str) -> str:
    """Label an address by how useful it is for sending an application.

    "hiring" is a recruiting mailbox (careers@, hr@, talent@); "person" looks like
    an individual (priya.sharma@); "general" is a shared inbox (info@, hello@);
    anything else is "other".
    """
    local = email.split("@", 1)[0].lower()
    tokens = _local_tokens(local)
    if local in _HIRING_TOKENS or any(token in _HIRING_TOKENS for token in tokens):
        return "hiring"
    # Run-together mailboxes: "askhr", "hrindia", "talentacquisitionindia", "joinourteam".
    # A leading "hr" only counts before a known word, so "hrithik@" stays a person.
    if any(word in local for word in _HIRING_SUBSTRINGS) or local.endswith("hr") or re.match(
        r"^hr(india|team|dept|desk|ops|help|admin|mgr|manager|head|recruit|global|us|uk|in)?$", local
    ):
        return "hiring"
    if local in _GENERAL_TOKENS or any(token in _GENERAL_TOKENS for token in tokens):
        return "general"
    # A name-shaped local part: letters, optionally split once by a separator.
    if re.fullmatch(r"[a-z]{2,}(?:[._-][a-z]{1,})?", local):
        return "person"
    return "other"


def _is_plausible(email: str) -> bool:
    """Reject template placeholders, tooling addresses and asset filenames."""
    local, _, domain = email.lower().partition("@")
    if not local or not domain:
        return False
    if domain.endswith(_ASSET_SUFFIXES) or local.endswith(_ASSET_SUFFIXES):
        return False
    if local in _JUNK_LOCAL_PARTS:
        return False
    tokens = _local_tokens(local)
    if tokens and not any(token in _HIRING_TOKENS for token in tokens) and any(
        token in _NOT_FOR_RESUMES for token in tokens
    ):
        return False
    # Words that disqualify a mailbox wherever they appear, even run together
    # ("reportfraud@") or next to a hiring word ("disabilityrecruitment@" handles
    # accommodation requests, not applications).
    if any(word in local for word in _NEVER_FOR_RESUMES):
        return False
    if any(domain == junk or domain.endswith("." + junk) for junk in _JUNK_DOMAINS):
        return False
    if len(local) > 64 or len(email) > 254:
        return False
    # Hash-like local parts are tracking IDs ("a1b2c3d4e5f6@sentry...").
    if re.fullmatch(r"[0-9a-f]{16,}", local):
        return False
    return True


def extract_emails(text: Optional[str]) -> List[Tuple[str, str]]:
    """Find published email addresses in text or HTML.

    Returns `(email, kind)` pairs, lowercased, de-duplicated, ordered by
    usefulness for sending a resume and then by first appearance.
    """
    if not text:
        return []
    content = html.unescape(str(text))
    # `mailto:` links carry the address even when the visible text differs.
    content = re.sub(r"mailto:", " ", content, flags=re.IGNORECASE)

    found: Dict[str, int] = {}

    def add(candidate: str, position: int) -> None:
        email = candidate.strip().strip(".").lower()
        if _is_plausible(email) and email not in found:
            found[email] = position

    for match in _EMAIL_RE.finditer(content):
        add(f"{match.group(1)}@{match.group(2)}", match.start())

    for match in _OBFUSCATED_RE.finditer(content):
        domain = re.sub(_DOT, ".", match.group(2), flags=re.IGNORECASE)
        candidate = f"{match.group(1)}@{domain}"
        if _EMAIL_RE.fullmatch(candidate):
            add(candidate, match.start())

    ordered = sorted(found.items(), key=lambda item: (KIND_RANK[classify_email(item[0])], item[1]))
    return [(email, classify_email(email)) for email, _ in ordered]


def job_post_contacts(description: Optional[str], listed_emails=None) -> List[dict]:
    """Contacts published in a job post, as `JobContact`-shaped dicts.

    `listed_emails` is what the board itself extracted (JobSpy's `emails`
    column); it is merged with a scan of the description, which also decodes
    obfuscated addresses the board's extractor misses.
    """
    candidates: List[str] = []
    if listed_emails:
        if isinstance(listed_emails, str):
            listed_emails = re.split(r"[,;\s]+", listed_emails)
        candidates.extend(str(item) for item in listed_emails if item)
    text = " ".join(candidates) + "\n" + (description or "")
    return [
        {"email": email, "kind": kind, "source": "job_post"}
        for email, kind in extract_emails(text)
    ]


def registrable_domain(host: Optional[str]) -> str:
    """Reduce a hostname to the part a company actually owns.

    "careers.acme.co.in" -> "acme.co.in"; "jobs.acme.com" -> "acme.com". Used to
    keep only addresses that belong to the company whose site was crawled.
    """
    if not host:
        return ""
    host = host.lower().strip().strip(".")
    host = re.sub(r"^https?://", "", host).split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) <= 2:
        return host
    second_level = {"co", "com", "net", "org", "ac", "gov", "edu", "ltd", "plc"}
    if parts[-2] in second_level and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])
