"""Does a job title actually name one of the roles being searched for?

LinkedIn and Indeed match a query loosely. A live Bengaluru sweep for
"Associate Product Manager", "AI Product Manager" and "AI ML Engineer" returned
Business Analyst, Merchandiser and Credit Risk Associate roles: 154 of 379
postings. Each one costs an LLM evaluation, and an LLM judge can still wave a
wrong role through (the credit-risk role scored 7.5 on overlapping Python
skills).

A title is relevant when it contains every *core* word of a target role.
Seniority and rank words are not core, so "Associate Product Manager" also
finds "Product Manager" and "Senior Product Manager". The spellings of AI and ML
are folded together, so "AI ML Engineer" finds "Machine Learning Engineer" and
"GenAI Engineer".
"""

from __future__ import annotations

import re
from typing import Iterable, List, Set

from job_agent.config.normalize import clean_text

# Rank words: they place a role on a ladder but do not say what the role is.
_SENIORITY = {
    "associate", "senior", "sr", "junior", "jr", "lead", "principal", "staff", "head", "chief", "intern",
    "trainee", "entry", "level", "mid", "i", "ii", "iii", "iv", "1", "2", "3", "the", "of", "and", "&", "-",
}

# Phrases rewritten to one token before comparison, longest first.
_PHRASES = [
    (r"artificial intelligence", "ai"),
    (r"machine learning", "ai"),
    (r"deep learning", "ai"),
    (r"gen\s*ai|generative ai", "ai"),
    (r"\bllms?\b", "ai"),
    (r"\bml\b", "ai"),
    (r"\bnlp\b", "ai"),
    (r"\bai\s*/\s*ai\b", "ai"),
    (r"\bagentic\b", "ai"),
    # Specialisms with no "ai"/"ml" token of their own, and the scientist titles
    # used for the same work. Only AI/applied scientists are folded (a plain data scientist is analytics work, which an existing test pins as a non-match), so an
    # unrelated scientist (a lab or research role) never becomes a match.
    (r"\b(?:ml|llm|genai|ai)\s*ops\b", "ai"),
    (r"\bcomputer vision\b|\bgenerative models?\b|\brag\b", "ai"),
    (r"\b(?:ai|applied)\s+scientists?\b", "ai engineer"),
    (r"\bmgr\b", "manager"),
    (r"\bengg\b", "engineer"),
    # An "AI Developer" does the job of an "AI Engineer".
    (r"\bdev\b|\bdeveloper\b", "engineer"),
    (r"\bproduct (owner|lead)\b", "product manager"),
]


def _words(text: str) -> List[str]:
    lowered = clean_text(text).casefold()
    for pattern, replacement in _PHRASES:
        lowered = re.sub(pattern, f" {replacement} ", lowered)
    words = re.findall(r"[a-z0-9+#]+", lowered)
    # Plural and singular forms name the same role.
    return [word[:-1] if len(word) > 4 and word.endswith("s") and not word.endswith("ss") else word
            for word in words]


def core_words(role: str) -> Set[str]:
    """The words a title must contain to be this role."""
    words = set(_words(role)) - _SENIORITY
    return words or set(_words(role))


_FAMILIES = {"sale", "recruiter", "recruitment", "marketing", "presale"}


def title_matches(title: str, roles: Iterable[str]) -> bool:
    """Whether a posting title names any of the target roles."""
    title_words = set(_words(title))
    # Shared AI/product keywords do not turn a sales or recruiting role into an
    # engineering/product role. Only the job itself counts: in "AI Engineer,
    # Marketing Platform" the text after the comma names a team, not the role.
    job_part = re.split(r"[,|(]|\s[-\u2013\u2014:]\s", clean_text(title), maxsplit=1)[0]
    role_words = set(_words(job_part))
    return any(core and core <= title_words and not ((role_words & _FAMILIES) - core)
               for core in (core_words(role) for role in roles))


# Words a role name is normally built from; a near-miss to one of these is a typo.
_VOCABULARY = {
    "engineer", "engineering", "developer", "scientist", "researcher", "research", "analyst", "manager",
    "architect", "specialist", "consultant", "learning", "machine", "artificial", "intelligence",
    "generative", "agentic", "applied", "senior", "principal", "software", "product", "data", "vision",
    "computer", "language", "natural", "processing", "automation", "platform", "backend", "frontend",
    "fullstack", "associate", "assistant", "designer", "director", "intern", "internship", "operations",
}


def suspicious_role_words(role: str) -> List[tuple]:
    """(word, likely_spelling) pairs for words in a role that look like typos.

    A misspelled target role matches nothing, and nothing says so. Only words
    that are *almost* a known role word are flagged, so unusual valid words
    (Kubernetes, Quant, Robotics) are left alone.
    """
    import difflib

    flagged = []
    for word in re.findall(r"[a-z]+", clean_text(role).casefold()):
        if len(word) < 5 or word in _VOCABULARY:
            continue
        close = difflib.get_close_matches(word, _VOCABULARY, n=1, cutoff=0.85)
        if close:
            flagged.append((word, close[0]))
    return flagged
