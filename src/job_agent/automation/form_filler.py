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

    def salary_answer(self, lowered_label: str, numeric: bool = False) -> Optional[str]:
        """The salary expectation in the form the field asks for.

        A number field, or a label asking in lakhs, gets a single figure (the top
        of the range: a form keeps one number, and the range's bottom would cap
        the offer). A free-text field gets the range as the candidate stated it.
        """
        profile = self.profile
        if profile.desired_salary is None:
            return None
        top = profile.desired_salary_max or profile.desired_salary
        if "lpa" in lowered_label or "lakh" in lowered_label or "lac" in lowered_label:
            return f"{top / 100000:g}"
        if numeric:
            return str(top)
        return profile.salary_expectation_text()

    def answer_screening_question(self, question_text: str) -> Optional[str]:
        """Answer a screening question from profile facts, or return None if unknown.

        Returning None (rather than a guess) is what lets the caller leave the
        field for the human-in-the-loop pause instead of submitting a fabricated
        answer about eligibility or compensation.
        """
        question = clean_text(question_text)
        lowered = question.lower()
        authorization = self.profile.work_authorization

        # Work authorization: answered from the profile, not assumed. A question
        # that names no country is about the job's own location.
        if any(phrase in lowered for phrase in ("authorized to work", "legally authorized", "right to work", "eligible to work")):
            authorized = authorization.is_authorized_in(question)
            if authorized is None:
                authorized = authorization.is_authorized_in(self.job.location)
            if authorized is None and self.job.is_remote and authorization.remote_worldwide:
                authorized = True
            if authorized is None:
                return None
            return _YES if authorized else _NO

        if "sponsorship" in lowered or "visa" in lowered:
            from job_agent.config.schema import location_country

            place = question if location_country(question) else self.job.location
            needed = authorization.needs_sponsorship_for(place, is_remote=self.job.is_remote and place != question)
            if needed is None:
                return None
            return _YES if needed else _NO

        if any(phrase in lowered for phrase in ("willing to work remote", "open to remote", "comfortable working remote")):
            return None if authorization.remote_worldwide is None else (_YES if authorization.remote_worldwide else _NO)

        if "current ctc" in lowered or "current salary" in lowered or "current compensation" in lowered:
            # Not something the candidate has stated; never inferred from the expectation.
            return None

        if "18 years" in lowered or "at least 18" in lowered or "age of 18" in lowered:
            # Derivable from the profile only if education or work history implies it;
            # every candidate with professional experience qualifies.
            return None

        if "years of experience" in lowered or "years experience" in lowered:
            if any(word in lowered for word in (" with ", " in ", " using ")):
                return None
            return str(int(self.profile.years_of_experience))

        if any(phrase in lowered for phrase in (
            "desired salary", "salary expectation", "expected salary", "expected compensation",
            "compensation expectation", "expected ctc", "salary requirement", "expected pay",
        )):
            return self.salary_answer(lowered)

        if "notice period" in lowered or "start date" in lowered or "available to start" in lowered:
            return None

        # Open-ended questions: answer with an LLM grounded in profile facts.
        return self._llm_answer(question)

    def _llm_answer(self, question: str) -> Optional[str]:
        """Draft an open-ended answer from profile facts, or None if no LLM is configured."""
        from job_agent.automation.routing import is_workday
        if is_workday(self.job.apply_url) or is_workday(self.job.job_url):
            return None  # Workday screening answers require explicit profile fields.
        if settings.active_provider not in ("openai", "groq", "openai_compatible"):
            return None
        try:
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
            if settings.active_provider == "groq":
                from job_agent.llm import groq_complete
                return groq_complete("Answer only from supplied candidate facts. Do not guess.",
                                     prompt, json_mode=False, max_tokens=512)
            from openai import OpenAI
            if settings.active_provider == "openai_compatible":
                client = OpenAI(
                    api_key=settings.openai_compatible_api_key,
                    base_url=settings.openai_compatible_base_url,
                )
                model = settings.openai_compatible_model
            else:
                client = OpenAI(api_key=settings.openai_api_key)
                model = settings.llm_tailor_model
            response = client.chat.completions.create(
                model=model,
                temperature=0.2,
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}],
            )
            return clean_text(response.choices[0].message.content) or None
        except Exception as exc:
            console.print(f"[dim]Could not draft an answer for '{question[:50]}': {exc}[/dim]")
            return None

    # --- Field dispatch -------------------------------------------------------

    @staticmethod
    def _is_search_field(field: Dict[str, Any]) -> bool:
        """Whether a field belongs to a site search box rather than the application."""
        if (field.get("type") or "").lower() == "search":
            return True
        if (field.get("name") or "").lower() in ("q", "query", "keywords"):
            return True
        haystack = " ".join(
            str(field.get(key) or "") for key in ("name", "id", "placeholder", "aria_label")
        ).lower()
        return any(marker in haystack for marker in ("search", "keyword", "typeahead"))

    def fill_field(self, field: Dict[str, Any], pdf_resume_path: Optional[Path] = None) -> bool:
        """Fill one detected form field, returning whether anything was entered."""
        locator: Locator = field["locator"]
        label = (field.get("label") or "").lower()
        field_type = field.get("type", "text")
        tag = field.get("tag", "input")
        contact = self.profile.contact
        first_name, last_name = self._split_name()

        descriptor = ' '.join(str(field.get(k) or '') for k in ('label', 'name', 'id', 'aria_label')).lower()
        if any(term in descriptor for term in ('gender', 'race', 'ethnic', 'veteran', 'disabilit', 'sexual orientation', 'demographic')):
            # Never infer sensitive disclosures, even from other profile text.
            return self._skip(label, 'voluntary disclosure requires your own choice')

        if field_type in ('password', 'hidden'):
            return False

        if field.get('role') == 'combobox' and tag not in ('input', 'select'):
            answer = (self.profile.work_authorization.current_country if label.strip(' *') in ('country', 'country of residence')
                      else self.answer_screening_question(label))
            if answer == 'Unspecified':
                answer = None
            if not answer:
                return self._skip(label, 'no profile fact determines this dropdown')
            locator.click()
            option = locator.page.get_by_role('option', name=answer, exact=True)
            if option.count() == 1 and option.is_visible():
                option.click()
                return True
            locator.press('Escape')
            return self._skip(label, 'no exact matching dropdown option')

        if self._is_search_field(field):
            # Job sites put a search bar ("Location", "Keywords") above the form;
            # filling it with profile data navigates away or corrupts the page.
            return False

        try:
            if field_type == "file":
                if 'cover' in descriptor:
                    from job_agent.tracking.supplements import document_links
                    letter = document_links(profile_hash=self.profile.profile_hash).get(self.job.id, {}).get('Cover Letter')
                    if not letter:
                        return self._skip(label, 'no validated cover letter was requested')
                    locator.set_input_files(letter)
                    return True
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

            # --- Salary expectation ---
            if any(token in label for token in ("expected salary", "expected ctc", "salary expectation",
                                                 "desired salary", "expected compensation")) and tag != "select":
                answer = self.salary_answer(label, numeric=field_type == "number")
                if not answer:
                    return self._skip(label, "no salary expectation in profile")
                locator.fill(answer)
                return True

            # --- Country of residence ---
            if label.strip("* ") in ("country", "country of residence", "current country") and tag != "select":
                country = self.profile.work_authorization.current_country
                if not country or country == "Unspecified":
                    return self._skip(label, "profile has no country")
                locator.fill(country)
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
