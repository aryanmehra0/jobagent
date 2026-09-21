"""Dynamic resume tailoring and bullet rewriting.

Re-weights and rephrases the candidate's bullets and summary toward a target job,
then runs a two-sided integrity gate over the result:

* **Restoration** - every locked metric that the rewrite dropped is put back, so
  tailoring can never quietly delete the candidate's strongest evidence.
* **Fabrication removal** - any metric in the rewritten text that does *not* exist
  in the sealed profile is stripped, so tailoring can never invent evidence either.

The second half is the one that matters most: an LLM told to "emphasize impact"
will happily upgrade "reduced costs" to "reduced costs by 60%", and that number
would otherwise be printed on a PDF sent to a real employer.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console

from job_agent.config.normalize import clean_text, dedupe_preserving_order
from job_agent.config.schema import CandidateProfile, JobPosting, WorkExperience
from job_agent.config.settings import settings
from job_agent.intake.validator import extract_potential_metrics

console = Console()

TAILORING_SYSTEM_PROMPT = """You are an expert ATS optimization consultant and executive resume writer.
Your mission is to tailor the candidate's resume bullet points and summary specifically for the target job.

CRITICAL ANTI-HALLUCINATION & FACT PRESERVATION RULES:
1. PRESERVE ALL ORIGINAL METRICS AND NUMBERS VERBATIM:
   Every metric, percentage, dollar value, scale indicator, and timeline ($340k, 200k RPS, 45%, 99.999% SLA) present in the candidate profile MUST be retained character-for-character.
2. ZERO FABRICATION: Do NOT invent new metrics, numbers, tools, employers, degrees, or years of experience. If a bullet has no number, it must still have no number after rewriting.
3. EVIDENCE-PRESERVING OPTIMIZATION:
   Rank the existing bullet points by relevance. COPY each selected bullet VERBATIM from the candidate profile. Do not rewrite it or introduce job-description claims into candidate achievements.
4. TAILOR THE PROFESSIONAL SUMMARY:
   Copy the candidate's original summary verbatim.
5. REORDER BULLETS: Frontload the achievements that most directly address the job requirements.
6. Return one entry per role in the candidate profile, echoing its company, title, and start_date exactly as given so the roles can be matched back.

Output ONLY valid JSON adhering to this schema:
{
  "tailored_summary": "...",
  "tailored_experience": [
    {"company": "...", "title": "...", "start_date": "...", "tailored_bullets": ["..."]}
  ]
}
"""


_TERM_STOPWORDS = {
    "the", "and", "for", "with", "you", "our", "your", "are", "will", "have", "has", "this", "that", "from", "their",
    "who", "work", "team", "role", "job", "about", "all", "can", "able", "into", "across", "more", "etc", "using",
    "experience", "years", "strong", "good", "including", "such", "other", "within", "also", "must", "should",
}


def job_terms(job: JobPosting) -> Dict[str, float]:
    """Words of a posting weighted by where they appear: the title counts most."""
    weights: Dict[str, float] = {}
    for text, weight in ((job.title, 3.0), (job.description, 1.0)):
        for word in re.findall(r"[a-z][a-z0-9+#./-]{1,}", (text or "").lower()):
            word = word.strip("./-")
            if len(word) > 1 and word not in _TERM_STOPWORDS:
                weights[word] = weights.get(word, 0.0) + weight
    # Diminishing returns: a word repeated 30 times in boilerplate is not 30x relevant.
    return {word: min(weight, 6.0) for word, weight in weights.items()}


def relevance(text: str, terms: Dict[str, float]) -> float:
    words = set(re.findall(r"[a-z][a-z0-9+#./-]{1,}", (text or "").lower()))
    return sum(terms.get(word.strip("./-"), 0.0) for word in words)


def skill_in_posting(skill: str, job: JobPosting) -> bool:
    haystack = f"{job.title} {job.description}".lower()
    return re.search(rf"(?<![a-z0-9]){re.escape(skill.lower())}(?![a-z0-9])", haystack) is not None


def order_by_relevance(items: List[Any], key, terms: Dict[str, float]) -> List[Any]:
    """Stable sort, most relevant first; ties keep the candidate's own order."""
    return [item for _, _, item in sorted(
        ((-relevance(key(item), terms), index, item) for index, item in enumerate(items)),
        key=lambda entry: (entry[0], entry[1]),
    )]


class ResumeTailorer:
    """Orchestrates dynamic ATS resume tailoring with strict fact-lock verification."""

    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or settings.active_provider).lower()

    # --- Provider adapters ----------------------------------------------------

    @staticmethod
    def _build_prompt(candidate_json: str, job: JobPosting) -> str:
        """Assemble the user turn pairing the target posting with the truth source."""
        return (
            f"=== TARGET JOB POSTING ===\n"
            f"Title: {job.title}\n"
            f"Company: {job.company}\n"
            f"Description:\n{job.description[:2500]}\n\n"
            f"=== CANDIDATE PROFILE (TRUTH SOURCE) ===\n"
            f"{candidate_json}\n\n"
            f"Tailor the resume summary and bullet points. Output ONLY valid JSON."
        )

    def _call_openai(self, candidate_json: str, job: JobPosting) -> Dict[str, Any]:
        """Tailor via the OpenAI Chat Completions API in JSON mode."""
        from openai import OpenAI

        if self.provider == "openai_compatible":
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
            response_format={"type": "json_object"},
            temperature=0.2,
            messages=[
                {"role": "system", "content": TAILORING_SYSTEM_PROMPT},
                {"role": "user", "content": self._build_prompt(candidate_json, job)},
            ],
        )
        return json.loads(response.choices[0].message.content)

    def _call_anthropic(self, candidate_json: str, job: JobPosting) -> Dict[str, Any]:
        """Tailor via the Anthropic Messages API."""
        import anthropic

        from job_agent.intake.parser import _extract_json_object

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=4000,
            temperature=0.2,
            system=TAILORING_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": self._build_prompt(candidate_json, job)}],
        )
        return _extract_json_object(response.content[0].text)

    # --- Deterministic tailoring ---------------------------------------------

    def _call_groq(self, candidate_json, job):
        from job_agent.llm import groq_complete
        return groq_complete(TAILORING_SYSTEM_PROMPT, self._build_prompt(candidate_json, job), max_tokens=6000)

    def _heuristic_tailor(self, profile: CandidateProfile, job: JobPosting) -> Dict[str, Any]:
        """Deterministic tailoring: reorder bullets by relevance without rewording them.

        Reordering is safe where rewording is not, so the offline path re-ranks
        bullets against the posting's keywords and leaves their text untouched.
        """
        job_keywords = set(re.findall(r"\b[a-z]{3,}\b", f"{job.title} {job.description}".lower()))

        tailored_experience = []
        for exp in profile.experience:
            scored = []
            for bullet in exp.description_bullets:
                bullet_words = set(re.findall(r"\b[a-z]{3,}\b", bullet.lower()))
                # Ties keep the resume's own order, which is usually chronological impact order.
                scored.append((len(bullet_words & job_keywords), bullet))
            scored.sort(key=lambda item: item[0], reverse=True)
            tailored_experience.append({
                "company": exp.company,
                "title": exp.title,
                "start_date": exp.start_date,
                "tailored_bullets": [bullet for _, bullet in scored],
            })

        top_skills = ", ".join(profile.skills.all_skills()[:6])
        tailored_summary = (
            f"{profile.summary} Applying for {job.title} at {job.company}, "
            f"bringing {profile.years_of_experience:g} years of experience across {top_skills}."
        )

        return {"tailored_summary": tailored_summary, "tailored_experience": tailored_experience}

    # --- Integrity gate -------------------------------------------------------

    @staticmethod
    def _match_role(
        entry: Dict[str, Any],
        roles: List[WorkExperience],
        used: set,
    ) -> Optional[WorkExperience]:
        """Match a tailored entry back to its source role.

        Matching is by (company, title, start_date) and then by company alone,
        with each role consumed once. Keying by company alone would merge two
        roles at the same employer, attaching a promotion's bullets to the wrong title.
        """
        company = clean_text(entry.get("company")).casefold()
        title = clean_text(entry.get("title")).casefold()
        start = clean_text(entry.get("start_date")).casefold()

        for role in roles:
            if role.key in used:
                continue
            if role.key == (company, title, start):
                return role
        for role in roles:
            if role.key in used:
                continue
            if role.company.casefold() == company and role.title.casefold() == title:
                return role
        for role in roles:
            if role.key in used:
                continue
            if role.company.casefold() == company:
                return role
        return None

    def enforce_metric_integrity(
        self,
        profile: CandidateProfile,
        tailored_data: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], List[str], List[str]]:
        """Restore dropped locked metrics and strip invented ones.

        Returns:
            (repaired data, metrics restored, fabricated metrics removed)
        """
        allowed = self._allowed_metrics(profile)
        entries = tailored_data.get("tailored_experience") or []
        by_role: Dict[Tuple[str, str, str], List[str]] = {}
        used: set = set()

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            role = self._match_role(entry, profile.experience, used)
            if role is None:
                continue
            used.add(role.key)
            bullets = [clean_text(item) for item in (entry.get("tailored_bullets") or []) if clean_text(item)]
            by_role[role.key] = bullets or list(role.description_bullets)

        restored: List[str] = []
        fabricated: List[str] = []

        for role in profile.experience:
            bullets = by_role.get(role.key, list(role.description_bullets))

            # 1. Strip metrics the sealed profile does not contain.
            cleaned_bullets = []
            for bullet in bullets:
                cleaned, invented = self._strip_unverified_metrics(bullet, allowed, role)
                fabricated.extend(invented)
                if cleaned:
                    cleaned_bullets.append(cleaned)
            bullets = cleaned_bullets or list(role.description_bullets)

            # 2. Restore any locked metric the rewrite lost.
            blob = self._normalize(" ".join(bullets))
            for fact in role.locked_facts:
                for metric in fact.metrics():
                    if self._normalize(metric) not in blob:
                        console.print(f"[yellow]Restoring locked metric '{metric}' for {role.company}[/yellow]")
                        bullets.append(fact.statement)
                        restored.append(metric)
                        blob = self._normalize(" ".join(bullets))

            by_role[role.key] = dedupe_preserving_order(bullets)

        tailored_data["tailored_experience"] = [
            {
                "company": role.company,
                "title": role.title,
                "start_date": role.start_date,
                "tailored_bullets": by_role.get(role.key, list(role.description_bullets)),
            }
            for role in profile.experience
        ]
        return tailored_data, restored, fabricated

    @staticmethod
    def _allowed_metrics(profile: CandidateProfile) -> set:
        """Every metric the profile can vouch for, in normalized form.

        Drawn from the locked facts *and* from the original bullet and project
        text. The two can disagree — locked facts may have been written by an LLM
        or by an earlier version of the extractor — and treating only the locked
        values as truth would flag the candidate's own untouched bullets as
        fabrications.
        """
        allowed = {ResumeTailorer._normalize(m) for m in profile.all_locked_metrics()}
        for exp in profile.experience:
            for bullet in exp.description_bullets:
                allowed.update(ResumeTailorer._normalize(m) for m in extract_potential_metrics(bullet))
        for project in profile.projects:
            allowed.update(ResumeTailorer._normalize(m) for m in extract_potential_metrics(project.description))
        allowed.update(ResumeTailorer._normalize(m) for m in extract_potential_metrics(profile.summary))
        return {value for value in allowed if value}

    @staticmethod
    def _is_supported(metric: str, allowed: set) -> bool:
        """Whether a metric in a rewrite is backed by the profile.

        Containment counts in both directions: a rewrite that says "50M events
        daily" where the profile recorded "50M events" is reporting the same
        figure with different wording, which is exactly what tailoring is for.
        Only a genuinely new number fails this test.
        """
        candidate = ResumeTailorer._normalize(metric)
        if not candidate:
            return True
        return any(candidate in value or value in candidate for value in allowed)

    def _strip_unverified_metrics(
        self,
        bullet: str,
        allowed: set,
        role: WorkExperience,
    ) -> Tuple[str, List[str]]:
        """Reject a bullet whose numbers do not appear anywhere in the sealed profile.

        The bullet is replaced with the original it was rewritten from. When no
        original is recognisable, the bullet is dropped outright rather than
        patched: excising a number leaves a mangled claim ("reduced costs by ."),
        and a rewrite built on an invented figure is not trustworthy anyway. The
        restoration pass that follows puts the candidate's real bullets back.
        """
        invented = [
            metric
            for metric in extract_potential_metrics(bullet)
            if not self._is_supported(metric, allowed)
        ]
        if not invented:
            return bullet, []

        original = self._closest_original(bullet, role.description_bullets)
        action = "reverting to the original bullet" if original else "dropping the bullet"
        console.print(
            f"[bold yellow]Fabrication gate:[/bold yellow] unverified metric(s) "
            f"{', '.join(invented)} in a {role.company} bullet; {action}."
        )
        return (original or ""), invented

    @staticmethod
    def _closest_original(bullet: str, originals: List[str]) -> Optional[str]:
        """Find the original bullet a rewrite most likely came from.

        Two shared content words is enough: resume bullets are short, and a
        rewrite deliberately swaps synonyms, so demanding more would fail to
        recognize exactly the rewrites this gate exists to catch.
        """
        if not originals:
            return None
        target = set(re.findall(r"\b[a-z]{4,}\b", bullet.lower()))
        if not target:
            return None
        best, best_overlap = None, 0
        for candidate in originals:
            overlap = len(target & set(re.findall(r"\b[a-z]{4,}\b", candidate.lower())))
            if overlap > best_overlap:
                best, best_overlap = candidate, overlap
        return best if best_overlap >= 2 else None

    @staticmethod
    def _normalize(text: str) -> str:
        """Collapse whitespace, commas, and case so metric comparison is format-insensitive."""
        return re.sub(r"[\s,]", "", clean_text(text)).lower()

    # Retained under the original private name for backwards compatibility with
    # callers and tests written against the previous API.
    def _verify_and_enforce_metric_integrity(
        self,
        original_profile: CandidateProfile,
        tailored_data: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Backwards-compatible wrapper around `enforce_metric_integrity`."""
        repaired, _, _ = self.enforce_metric_integrity(original_profile, tailored_data)
        return repaired

    # --- Orchestration --------------------------------------------------------

    def generate_tailored_profile_data(
        self,
        profile: CandidateProfile,
        job: JobPosting,
        use_llm: bool = True,
    ) -> Dict[str, Any]:
        """Produce the tailored JSON payload the Typst template compiles."""
        console.print(f"[cyan]Tailoring resume for:[/cyan] [bold]{job.title}[/bold] @ {job.company}")

        tailored_response: Optional[Dict[str, Any]] = None
        if use_llm and self.provider in ("openai", "anthropic", "groq", "openai_compatible"):
            caller = {"openai": self._call_openai, "anthropic": self._call_anthropic,
                      "groq": self._call_groq, "openai_compatible": self._call_openai}[self.provider]
            try:
                tailored_response = caller(profile.model_dump_json(indent=2), job)
            except Exception as exc:
                if settings.llm_strict:
                    raise
                console.print(
                    f"[yellow]{self.provider} tailoring failed ({exc.__class__.__name__}: "
                    f"{str(exc)[:120]}). Using the deterministic engine.[/yellow]"
                )

        if not isinstance(tailored_response, dict) or not tailored_response.get("tailored_experience"):
            tailored_response = self._heuristic_tailor(profile, job)

        verified, restored, fabricated = self.enforce_metric_integrity(profile, tailored_response)

        bullets_by_role = {
            (entry["company"].casefold(), entry["title"].casefold(), (entry.get("start_date") or "").casefold()): entry["tailored_bullets"]
            for entry in verified["tailored_experience"]
        }
        terms = job_terms(job)

        # The model may rank evidence, but novel prose cannot be proven true by
        # checking numbers alone. Restore exact source bullets for every rewrite,
        # then use deterministic job-term relevance for anything the model did
        # not validly select.
        for role in profile.experience:
            proposed = bullets_by_role.get(role.key, [])
            originals = list(role.description_bullets)
            selected = [bullet for bullet in proposed if bullet in originals]
            ranked = order_by_relevance(originals, lambda bullet: bullet, terms)
            bullets_by_role[role.key] = dedupe_preserving_order(selected + ranked)

        summary = profile.summary

        # Skills the posting names come first in each group, then the rest in the
        # candidate's order. Nothing is added or removed.
        skills = {
            group: sorted(values, key=lambda skill, _order={v: i for i, v in enumerate(values)}:
                          (not skill_in_posting(skill, job), _order[skill]))
            if isinstance(values, list) else values
            for group, values in profile.skills.model_dump().items()
        }
        projects = order_by_relevance(
            [proj.model_dump() for proj in profile.projects],
            lambda proj: " ".join([proj.get("title") or "", proj.get("description") or "",
                                   " ".join(proj.get("technologies") or [])]),
            terms,
        )

        from job_agent.tailoring.regional import regional_policy

        return {
            "regional": regional_policy(job),
            "contact": profile.contact.model_dump(),
            "summary": summary,
            "skills": skills,
            "experience": [
                {
                    "company": exp.company,
                    "title": exp.title,
                    "location": exp.location,
                    "start_date": exp.start_date,
                    # The template prints this directly; never hand it a null.
                    "end_date": exp.end_date or "Present",
                    "description_bullets": bullets_by_role.get(exp.key, exp.description_bullets),
                }
                for exp in sorted(profile.experience, key=lambda role: (
                    bool(role.is_current or not role.end_date), role.end_date or "9999", role.start_date), reverse=True)
            ],
            "education": [edu.model_dump() for edu in profile.education],
            "projects": projects,
            # Certifications were previously assembled but never passed through, so
            # the template's certifications section could never render.
            "certifications": [cert.model_dump() for cert in profile.certifications],
            "integrity": {
                "fact_hash": profile.fact_hash,
                "restored_metrics": restored,
                "dropped_fabrications": dedupe_preserving_order(fabricated, casefold=False),
            },
            "target_job": {
                "id": job.id,
                "title": job.title,
                "company": job.company,
                "job_url": job.job_url,
            },
        }
