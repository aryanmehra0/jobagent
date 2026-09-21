"""Candidate preferences a resume does not state.

Country, work authorization, sponsorship and salary expectation rarely appear on
a resume, yet application forms ask for all of them. They are stated once by the
candidate, kept in `data/profiles/preferences.json`, and applied to the sealed
profile. Re-running intake on a new resume re-applies them, so they are never
lost and never have to be re-entered.

Only the preference fields are touched. Locked career facts are verified before
the profile is re-sealed, so this cannot be used to launder an edited fact.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import Field, field_validator, model_validator

from job_agent.config.normalize import clean_text, dedupe_preserving_order
from job_agent.config.schema import CandidateProfile, StrictModel
from job_agent.config.settings import settings


SAMPLE_RESUME_NAME = "sample_resume.pdf"


def choose_resume(folder: Optional[Path] = None) -> Optional[Path]:
    """The candidate's own resume: the newest real upload, never the demo while one exists.

    Alphabetical order used to decide, so a resume named "zara.pdf" lost to the
    bundled "sample_resume.pdf" and every later phase ran on the demo profile.
    """
    from job_agent.intake.parser import SUPPORTED_RESUME_SUFFIXES

    folder = folder or settings.raw_resumes_dir
    if not folder.is_dir():
        return None
    candidates = [path for path in folder.iterdir()
                  if path.is_file() and path.suffix.lower() in SUPPORTED_RESUME_SUFFIXES]
    real = [path for path in candidates if path.name.lower() != SAMPLE_RESUME_NAME]
    pool = real or candidates
    return max(pool, key=lambda path: path.stat().st_mtime) if pool else None


def preferences_path() -> Path:
    return settings.profile_path.parent / "preferences.json"


class CandidatePreferences(StrictModel):
    """What the candidate has told the agent about eligibility and pay."""

    current_country: Optional[str] = Field(None, min_length=2, max_length=80)
    authorized_countries: List[str] = Field(default_factory=list)
    citizenship: List[str] = Field(default_factory=list)
    requires_sponsorship: Optional[bool] = Field(
        None, description="Needs a visa to work in countries outside authorized_countries"
    )
    remote_worldwide: Optional[bool] = Field(None, description="Will work remotely for employers anywhere")
    desired_salary: Optional[int] = Field(None, ge=0, le=100_000_000)
    desired_salary_max: Optional[int] = Field(None, ge=0, le=100_000_000)
    salary_currency: Optional[str] = None

    @field_validator("current_country", mode="before")
    @classmethod
    def _clean_country(cls, value: Any) -> Optional[str]:
        return clean_text(value).title() or None if value is not None else None

    @field_validator("authorized_countries", "citizenship", mode="before")
    @classmethod
    def _clean_lists(cls, value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            value = value.split(",")
        return dedupe_preserving_order(clean_text(item).title() for item in value if clean_text(item))

    @field_validator("salary_currency", mode="before")
    @classmethod
    def _clean_currency(cls, value: Any) -> Optional[str]:
        return clean_text(value).upper() or None if value is not None else None

    @model_validator(mode="after")
    def _check(self) -> CandidatePreferences:
        if self.desired_salary is not None and self.desired_salary_max is not None \
                and self.desired_salary_max < self.desired_salary:
            raise ValueError("The top of the salary range is below the bottom.")
        if (self.desired_salary is not None) and not self.salary_currency:
            raise ValueError("Give a currency (for example INR) with the salary expectation.")
        return self


def load_preferences() -> Optional[CandidatePreferences]:
    path = preferences_path()
    if not path.is_file():
        return None
    return CandidatePreferences.model_validate(json.loads(path.read_text(encoding="utf-8")))


def apply_preferences(profile: CandidateProfile, preferences: CandidatePreferences) -> CandidateProfile:
    """A copy of the profile with the preferences applied and the seal renewed."""
    data = profile.model_dump()
    auth = data["work_authorization"]
    if preferences.current_country:
        auth["current_country"] = preferences.current_country
    if preferences.authorized_countries:
        auth["authorized_countries"] = preferences.authorized_countries
    if preferences.citizenship:
        auth["citizenship"] = preferences.citizenship
    for key in ("requires_sponsorship", "remote_worldwide"):
        if getattr(preferences, key) is not None:
            auth[key] = getattr(preferences, key)
    for key in ("desired_salary", "desired_salary_max", "salary_currency"):
        if getattr(preferences, key) is not None:
            data[key] = getattr(preferences, key)
    updated = CandidateProfile.model_validate(data)
    # Only preference fields changed; the fact seal is carried over unchanged
    # and the whole-profile hash is recomputed.
    updated.fact_hash = profile.fact_hash
    updated.profile_hash = updated.compute_profile_hash()
    return updated


def save_preferences(values: Dict[str, Any], profile_path: Optional[Path] = None) -> CandidateProfile:
    """Validate, store and apply preferences to the sealed profile on disk.

    Raises ValueError when the profile's locked facts no longer match their seal.
    """
    preferences = CandidatePreferences.model_validate(values)
    target = profile_path or settings.profile_path
    profile = CandidateProfile.model_validate(json.loads(target.read_text(encoding="utf-8")))
    if not profile.verify_integrity():
        raise ValueError("The profile's locked facts do not match their seal. Re-run intake first.")

    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(preferences.model_dump_json(indent=2), encoding="utf-8")

    updated = apply_preferences(profile, preferences)
    target.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    return updated


def reapply_saved_preferences(profile: CandidateProfile, output_path: Path) -> CandidateProfile:
    """Called after intake: carry stored preferences into the freshly parsed profile."""
    preferences = load_preferences()
    if preferences is None:
        return profile
    updated = apply_preferences(profile, preferences)
    output_path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
    return updated
