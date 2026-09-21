"""Conservative regional presentation rules; candidate facts are never localised."""
from __future__ import annotations

import re
from typing import Any

from job_agent.config.schema import COUNTRY_CODES, JobPosting, location_country


def target_countries(location: str | None) -> list[str]:
    """Retain multiple named markets instead of silently picking the first."""
    text = (location or "").casefold().strip()
    found = []
    for country, aliases in COUNTRY_CODES.items():
        names = (country,) + aliases
        if any(re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", text)
               for name in names if len(name) > 3 or text == name or name in {"us", "usa", "uk", "gb", "uae"}):
            found.append(country)
    inferred = location_country(location)
    if inferred and inferred not in found:
        found.append(inferred)
    if not found:
        for city, country in {"london": "uk", "berlin": "germany", "toronto": "canada",
                              "sydney": "australia", "melbourne": "australia", "dublin": "ireland",
                              "amsterdam": "netherlands", "paris": "france"}.items():
            if re.search(rf"\b{city}\b", text):
                found.append(country)
    return found


def regional_policy(job: JobPosting, country_override: str | None = None) -> dict[str, Any]:
    countries = target_countries(country_override or job.location)
    country = countries[0] if len(countries) == 1 else "international"
    letter = country in {"usa", "canada"}
    cv = country in {"uk", "ireland", "germany", "france", "netherlands"}
    return {
        "country": country,
        "basis": "user override" if country_override else (
            "job location" if len(countries) == 1 else "country unclear or multiple markets"),
        "paper": "us-letter" if letter else "a4",
        "document_label": "CV" if cv else "Resume",
        "summary_heading": "Professional Profile" if cv else "Professional Summary",
        "experience_heading": "Employment History" if cv else "Professional Experience",
        "language": "en",
        "font_size_pt": 11,
        "notes": [
            "English ATS format; employer-specific instructions take precedence.",
            "Original achievements, dates, qualifications and contact details are preserved.",
            "No photo, birth date, marital status, nationality or invented visa claims are added.",
            "A region selects presentation only; it does not establish work eligibility.",
        ],
    }


def remote_eligibility(job: JobPosting, current_country: str | None) -> tuple[str, str]:
    """Location evidence only. Unknown region/timezone restrictions need review."""
    if not job.is_remote:
        return "Review relocation / work authorization", "Role requires physical presence."
    countries = target_countries(job.location)
    home = target_countries(current_country)
    if countries:
        if not home:
            return "Needs review", "Set your country of residence to check this restriction."
        if home[0] in countries:
            return "Location matches", "Posting names your country; check the full employer requirements."
        return "Location restricted", f"Posting names {', '.join(countries)}; residence is {home[0]}."
    text = (job.location or "").casefold()
    if re.search(r"\b(worldwide|anywhere|global)\b", text):
        return "Worldwide advertised", "Confirm timezone, employment arrangement and employer requirements."
    return "Needs review", "Remote does not establish worldwide eligibility; check country and timezone limits."
