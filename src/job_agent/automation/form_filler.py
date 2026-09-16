"""Form auto-fill and dynamic screening question answering.

Maps the sealed candidate profile onto detected DOM fields and answers screening
questions from profile facts.

The governing rule is that **a field with no backing fact is left blank**. An
earlier revision filled missing details with placeholders — a stand-in LinkedIn
URL, an invented phone number, a hardcoded salary expectation, and an
unconditional "Yes" to work-authorization questions. Those values go into a real
employer's ATS under the candidate's name, so anything not derivable from the
profile is now skipped and reported instead.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from playwright.sync_api import Locator
from rich.console import Console

from job_agent.config.normalize import clean_text
from job_agent.config.schema import CandidateProfile, JobPosting
from job_agent.config.settings import settings

console = Console()

# Answers that must come from the profile, never from a default.
_YES = "Yes"
_NO = "No"


class FormFiller:
    """Populates application forms with candidate profile data and screening answers."""

    def __init__(self, profile: CandidateProfile, target_job: JobPosting):
        self.profile = profile
        self.job = target_job
        # Fields left blank because the profile has nothing to put in them.
        self.skipped_fields: List[str] = []

    # --- Profile-derived values -----------------------------------------------

    def _split_name(self) -> Tuple[str, str]:
        """Split the full name into (first, last)."""
        parts = self.profile.contact.full_name.strip().split()
        if len(parts) > 1:
            return parts[0], " ".join(parts[1:])
        return self.profile.contact.full_name, ""

    def _skip(self, label: str, reason: str) -> bool:
        """Record a field left blank and return False so the caller moves on."""
        self.skipped_fields.append(f"{label or 'unlabelled field'} ({reason})")
        console.print(f"  [dim]Left blank: {label or 'unlabelled field'} - {reason}[/dim]")
        return False

    # --- Screening questions --------------------------------------------------

    def answer_screening_question(self, question_text: str) -> Optional[str]:
        """Answer a screening question from profile facts, or return None if unknown.

        Returning None (rather than a guess) is what lets the caller leave the
        field for the human-in-the-loop pause instead of submitting a fabricated
        answer about eligibility or compensation.
        """
        question = clean_text(question_text)
        lowered = question.lower()
        authorization = self.profile.work_authorization

        # Work authorization: answered from the profile, not assumed.
        if any(phrase in lowered for phrase in ("authorized to work", "legally authorized", "right to work", "eligible to work")):
            authorized = authorization.is_authorized_in(question) or authorization.is_authorized_in(self.job.location)
            if authorized is None:
                # The profile lists authorized countries; if the question does not
                # name one we cannot tell, so fall back to the general case.
                authorized = bool(authorization.authorized_countries) and not authorization.requires_sponsorship
            return _YES if authorized else _NO

        if "sponsorship" in lowered or "visa" in lowered:
            return _YES if authorization.requires_sponsorship else _NO

        if "18 years" in lowered or "at least 18" in lowered or "age of 18" in lowered:
            # Derivable from the profile only if education or work history implies it;
            # every candidate with professional experience qualifies.
            return _YES if self.profile.experience or self.profile.education else None

        if "years of experience" in lowered or "years experience" in lowered:
            return str(int(self.profile.years_of_experience))

        if any(phrase in lowered for phrase in ("desired salary", "salary expectation", "expected compensation", "compensation expectation")):
            if self.profile.desired_salary is None:
                return None
            return str(self.profile.desired_salary)

        if "notice period" in lowered or "start date" in lowered or "available to start" in lowered:
            return None

        # Open-ended questions: answer with an LLM grounded in profile facts.
        return self._llm_answer(question)

    def _llm_answer(self, question: str) -> Optional[str]:
        """Draft an open-ended answer from profile facts, or None if no LLM is configured."""
        if settings.active_provider != "openai":
            return None
        try:
            from openai import OpenAI

            client = OpenAI(api_key=settings.openai_api_key)
            prompt = (
                f"Candidate: {self.profile.contact.full_name}\n"
                f"Background: {self.profile.summary}\n"
                f"Skills: {', '.join(self.profile.skills.all_skills())}\n"
                f"Verified achievements: "
                f"{'; '.join(fact.statement for fact in self.profile.all_locked_facts()[:5])}\n"
                f"Target role: {self.job.title} at {self.job.company}\n\n"
                f"Question: {question}\n\n"
                "Answer in two sentences, in the first person, using ONLY the facts above. "
                "Do not invent employers, metrics, or credentials."
            )
            response = client.chat.completions.create(
                model=settings.llm_tailor_model,
                temperature=0.2,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return clean_text(response.choices[0].message.content) or None
        except Exception as exc:
            console.print(f"[dim]Could not draft an answer for '{question[:50]}': {exc}[/dim]")
            return None

    # --- Field dispatch -------------------------------------------------------

    def fill_field(self, field: Dict[str, Any], pdf_resume_path: Optional[Path] = None) -> bool:
        """Fill one detected form field, returning whether anything was entered."""
        locator: Locator = field["locator"]
        label = (field.get("label") or "").lower()
        field_type = field.get("type", "text")
        tag = field.get("tag", "input")
        contact = self.profile.contact
        first_name, last_name = self._split_name()

        try:
            if field_type == "file":
                if pdf_resume_path and Path(pdf_resume_path).exists():
                    console.print(f"  - Uploading tailored resume: [cyan]{Path(pdf_resume_path).name}[/cyan]")
                    locator.set_input_files(str(pdf_resume_path))
                    return True
                return self._skip(label, "no tailored resume PDF available")

            # --- Name ---
            if "first name" in label or label in ("firstname", "fname", "given name"):
                locator.fill(first_name)
                return True
            if "last name" in label or label in ("lastname", "lname", "surname", "family name"):
                if not last_name:
                    return self._skip(label, "profile has no surname")
                locator.fill(last_name)
                return True
            if "full name" in label or label == "name":
                locator.fill(contact.full_name)
                return True

            # --- Email ---
            if "email" in label or field_type == "email":
                locator.fill(contact.email)
                return True

            # --- Phone ---
            if "phone" in label or "mobile" in label or field_type == "tel":
                if not contact.phone:
                    return self._skip(label, "profile has no phone number")
                locator.fill(contact.phone)
                return True

            # --- Location ---
            if any(token in label for token in ("city", "location", "address", "town")):
                if not contact.location:
                    return self._skip(label, "profile has no location")
                locator.fill(contact.location)
                return True

            # --- Profile links ---
            if "linkedin" in label:
                if not contact.linkedin_url:
                    return self._skip(label, "profile has no LinkedIn URL")
                locator.fill(contact.linkedin_url)
                return True
            if "github" in label:
                if not contact.github_url:
                    return self._skip(label, "profile has no GitHub URL")
                locator.fill(contact.github_url)
                return True
            if any(token in label for token in ("portfolio", "website", "personal url")):
                if not contact.portfolio_url:
                    return self._skip(label, "profile has no portfolio URL")
                locator.fill(contact.portfolio_url)
                return True

            # --- Dropdowns ---
            if tag == "select":
                return self._fill_select(locator, label)

            # --- Checkboxes and radios ---
            if field_type in ("checkbox", "radio"):
                return self._fill_choice(locator, label)

            # --- Open-ended questions ---
            if tag == "textarea" or any(token in label for token in ("question", "why", "experience", "tell us", "describe")):
                answer = self.answer_screening_question(field.get("label") or "")
                if not answer:
                    return self._skip(label, "no profile fact answers this question")
                locator.fill(answer)
                return True

        except Exception as exc:
            console.print(f"[dim]Could not fill '{field.get('label')}': {exc}[/dim]")
            return False

        return False

    def _fill_select(self, locator: Locator, label: str) -> bool:
        """Choose a dropdown option that matches the profile's answer.

        A dropdown whose answer the profile does not determine is left at its
        default. The previous behaviour selected the first non-placeholder option,
        which silently answered eligibility questions at random.
        """
        answer = self.answer_screening_question(label)
        if not answer:
            return self._skip(label, "no profile fact determines this choice")

        try:
            options = locator.locator("option").all_inner_texts()
        except Exception:
            return False

        for option in options:
            if answer.strip().lower() == option.strip().lower():
                locator.select_option(label=option)
                return True
        for option in options:
            if answer.strip().lower() in option.strip().lower():
                locator.select_option(label=option)
                return True
        return self._skip(label, f"no option matched the profile answer '{answer}'")

    def _fill_choice(self, locator: Locator, label: str) -> bool:
        """Tick consent checkboxes; answer yes/no radios from the profile."""
        if any(token in label for token in ("agree", "terms", "consent", "privacy", "acknowledge")):
            locator.check()
            return True

        answer = self.answer_screening_question(label)
        if answer == _YES and re.search(r"\byes\b", label):
            locator.check()
            return True
        if answer == _NO and re.search(r"\bno\b", label):
            locator.check()
            return True
        return False
