"""Tier 2 semantic evaluation: sliding-window LLM re-ranker.

Scores candidate-to-job fit on a 1.0-10.0 scale using an LLM as a logical judge,
falling back to a deterministic heuristic judge when no API key is configured.

Two things make the score trustworthy rather than decorative:

* **The model's JSON is validated** against `RerankerVerdict` before use, so an
  out-of-range or malformed score is rejected instead of ranking a job.
* **`passed_threshold` is derived from the score**, never taken from the model.
  A judge that says "fit_score 4.0, passed: true" cannot push an unqualified role
  into the auto-apply queue.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console

from job_agent.config.normalize import clean_text, truncate
from job_agent.config.schema import (
    CandidateProfile,
    EvaluationScore,
    JobPosting,
    RerankerVerdict,
)
from job_agent.config.settings import settings

console = Console()

RERANKER_SYSTEM_PROMPT = """You are a rigorous, technical hiring evaluator and ATS screening auditor.
Your job is to objectively score the candidate's alignment with the provided job posting on a strict 1.0 to 10.0 scale.

EVALUATION CRITERIA:
1. Technical Stack Match (1.0 - 10.0): Do the candidate's verified skills, languages, and frameworks overlap with the required tech stack?
2. Seniority & Experience (1.0 - 10.0): Does the candidate have the requested years of experience and level of responsibility (Junior, Mid, Senior, Staff)?
3. Hard Constraints: Penalize only explicit contradictions in location or work authorization. Missing eligibility or relocation information is UNKNOWN, not evidence of ineligibility; flag it for human review in reasoning without inventing an answer or lowering the technical fit score.

SCORING RULES:
- 9.0 - 10.0: Exceptional match. Candidate meets >= 90% of core requirements and matches or exceeds the seniority asked for.
- 7.0 - 8.9: Strong match. Candidate meets the key requirements and qualifies for the role.
- 5.0 - 6.9: Moderate match. Substantial skill overlap, but notable gaps in critical frameworks or experience.
- 1.0 - 4.9: Weak match, or a disqualifying misalignment.

Judge only on evidence present in the candidate profile. Do not assume unlisted skills.
List a skill under matching_skills only if it appears in BOTH the profile and the posting.

Output ONLY a single valid JSON object adhering exactly to this schema:
{
  "fit_score": 7.5,
  "technical_score": 8.0,
  "seniority_score": 7.0,
  "reasoning": "Candidate possesses the Python and Kubernetes experience this role requires...",
  "matching_skills": ["Python", "Kubernetes", "AWS"],
  "missing_skills": ["Ruby on Rails", "Sidekiq"]
}
"""

# Sections of a posting that actually carry requirements, used to pick which
# sliding windows to send to the judge when a description is too long to send whole.
_REQUIREMENT_KEYWORDS = (
    "require", "requirement", "qualification", "you have", "you'll need", "must have",
    "experience with", "proficien", "expertise", "responsibilit", "what you", "skills",
    "years of experience", "nice to have", "preferred",
)

# Technologies checked for in a posting when reporting what the candidate lacks.
COMMON_TECH_VOCABULARY = (
    "kubernetes", "docker", "terraform", "ansible", "aws", "gcp", "azure", "python",
    "golang", "go", "rust", "java", "javascript", "typescript", "react", "angular",
    "vue", "node.js", "django", "flask", "fastapi", "spring", "rails", "php", "ruby",
    "c++", "c#", ".net", "scala", "kotlin", "swift", "sql", "postgresql", "mysql",
    "mongodb", "redis", "kafka", "rabbitmq", "elasticsearch", "spark", "hadoop",
    "airflow", "snowflake", "graphql", "grpc", "rest", "microservices", "ci/cd",
    "jenkins", "github actions", "prometheus", "grafana", "datadog", "pytorch",
    "tensorflow", "machine learning", "kubernetes operators", "helm", "linux",
)

# Budget for the job text sent to the judge; roughly 1500 tokens.
CONTEXT_BUDGET_CHARS = 6000



def _sponsorship_note(profile: CandidateProfile) -> str:
    """Sponsorship stated precisely, so a judge does not mark home-country jobs down."""
    auth = profile.work_authorization
    if auth.requires_sponsorship is None:
        return "unspecified"
    if not auth.requires_sponsorship:
        return "no"
    home = ", ".join(auth.authorized_countries) or auth.current_country or "home country"
    return f"no for jobs in {home} or remote jobs; yes only to relocate to another country"


class LLMReranker:
    """LLM-based logical judge re-ranking jobs that pass the Tier 1 embedding filter."""

    def __init__(self, provider: Optional[str] = None, min_score: Optional[float] = None):
        self.provider = (provider or settings.active_provider).lower()
        self.min_score = settings.min_match_score if min_score is None else min_score

    # --- Sliding window context selection -------------------------------------

    def _chunk_job_description(self, description: str, window_size: int = 1200) -> List[str]:
        """Split a long description into overlapping windows.

        Windows overlap by 25% so a requirement straddling a boundary is never cut
        in half and lost to the judge.
        """
        if len(description) <= window_size:
            return [description]

        chunks: List[str] = []
        current: List[str] = []
        current_len = 0

        for word in description.split():
            current.append(word)
            current_len += len(word) + 1
            if current_len >= window_size:
                chunks.append(" ".join(current))
                current = current[int(len(current) * 0.75):]
                current_len = sum(len(item) + 1 for item in current)

        if current:
            chunks.append(" ".join(current))
        return chunks

    def select_context(self, description: str) -> str:
        """Build the job text sent to the judge, prioritizing requirement-bearing windows.

        The original implementation only ever sent the first window, so a posting
        that opened with company boilerplate had its actual requirements scored
        against nothing. Windows are ranked by requirement-keyword density, the
        best ones are kept up to the context budget, and they are then restored to
        document order so the text still reads coherently.
        """
        description = description or ""
        if len(description) <= CONTEXT_BUDGET_CHARS:
            return description

        chunks = self._chunk_job_description(description)
        scored: List[Tuple[int, int, str]] = []
        for index, chunk in enumerate(chunks):
            lowered = chunk.lower()
            density = sum(lowered.count(keyword) for keyword in _REQUIREMENT_KEYWORDS)
            # The opening window is kept regardless: it carries the role summary.
            scored.append((density + (5 if index == 0 else 0), index, chunk))

        scored.sort(key=lambda item: item[0], reverse=True)

        selected: List[Tuple[int, str]] = []
        budget = CONTEXT_BUDGET_CHARS
        for _, index, chunk in scored:
            if budget <= 0:
                break
            selected.append((index, chunk[:budget]))
            budget -= len(chunk)

        selected.sort(key=lambda item: item[0])
        return "\n...\n".join(chunk for _, chunk in selected)

    # --- Provider adapters ----------------------------------------------------

    @staticmethod
    def _build_prompt(profile_summary: str, job: JobPosting, job_context: str) -> str:
        """Assemble the user turn describing the candidate and the posting."""
        return (
            f"=== CANDIDATE PROFILE ===\n{profile_summary}\n\n"
            f"=== TARGET JOB POSTING ===\n"
            f"Title: {job.title}\n"
            f"Company: {job.company}\n"
            f"Location: {job.location} (Remote: {job.is_remote})\n\n"
            f"Description:\n{job_context}"
        )

    def _call_openai(self, profile_summary: str, job: JobPosting, job_context: str) -> Dict[str, Any]:
        """Score via the OpenAI Chat Completions API in JSON mode."""
        from openai import OpenAI

        if self.provider == "openai_compatible":
            client = OpenAI(
                api_key=settings.openai_compatible_api_key,
                base_url=settings.openai_compatible_base_url,
            )
            model = settings.openai_compatible_model
        else:
            client = OpenAI(api_key=settings.openai_api_key)
            model = settings.llm_rerank_model
        response = client.chat.completions.create(
            model=model,
            response_format={"type": "json_object"},
            temperature=0.1,
            messages=[
                {"role": "system", "content": RERANKER_SYSTEM_PROMPT},
                {"role": "user", "content": self._build_prompt(profile_summary, job, job_context)},
            ],
        )
        return json.loads(response.choices[0].message.content)

    def _call_anthropic(self, profile_summary: str, job: JobPosting, job_context: str) -> Dict[str, Any]:
        """Score via the Anthropic Messages API."""
        import anthropic

        from job_agent.intake.parser import _extract_json_object

        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1500,
            temperature=0.1,
            system=RERANKER_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": self._build_prompt(profile_summary, job, job_context)
                    + "\n\nEvaluate and output ONLY the JSON object:",
                }
            ],
        )
        return _extract_json_object(response.content[0].text)

    # --- Deterministic judge --------------------------------------------------

    @staticmethod
    def _find_skills_in_text(skills: List[str], text: str) -> List[str]:
        """Which of the candidate's skills the posting mentions, in original casing.

        Word boundaries prevent "Go" matching "Google" and "R" matching every word
        containing the letter, which would inflate the technical score.
        """
        found: List[str] = []
        lowered = text.lower()
        for skill in skills:
            needle = skill.lower().strip()
            if len(needle) < 2:
                continue
            if re.search(rf"(?<![\w+#.]){re.escape(needle)}(?![\w+#])", lowered):
                found.append(skill)
        return found

    @staticmethod
    def _required_years(text: str) -> Optional[float]:
        """Extract the years of experience a posting asks for.

        Ranges ("3-5 years") resolve to the lower bound, which is the actual bar.
        Returns None when the posting does not state one, so the seniority score
        can stay neutral instead of being judged against an invented requirement.
        """
        range_match = re.search(r"\b(\d{1,2})\s*(?:-|to|–)\s*(\d{1,2})\+?\s*years?\b", text, re.IGNORECASE)
        if range_match:
            return float(range_match.group(1))
        single = re.search(r"\b(\d{1,2})\+?\s*years?\b", text, re.IGNORECASE)
        if single:
            years = float(single.group(1))
            # "20 years" in a paragraph about company history is not a requirement.
            return years if years <= 20 else None
        return None

    def _heuristic_reranker(
        self,
        profile: CandidateProfile,
        job: JobPosting,
        embedding_similarity: float,
    ) -> RerankerVerdict:
        """Deterministic judge used when no LLM provider is configured.

        Scores technical fit from verified skill overlap plus the Tier 1 similarity,
        and seniority from the gap between the candidate's tenure and the posting's
        stated requirement, then combines them 60/40.
        """
        candidate_skills = profile.skills.all_skills()
        haystack = f"{job.title} {job.description}"

        matching = self._find_skills_in_text(candidate_skills, haystack)

        owned = {skill.lower() for skill in candidate_skills}
        missing = [
            tech
            for tech in COMMON_TECH_VOCABULARY
            if tech not in owned and self._find_skills_in_text([tech], haystack)
        ]

        required_years = self._required_years(job.description)
        if required_years is None:
            seniority_score = 7.0
            seniority_note = "The posting does not state a years-of-experience requirement."
        else:
            gap = profile.years_of_experience - required_years
            if gap >= 0:
                seniority_score = min(10.0, 8.0 + gap * 0.5)
            else:
                seniority_score = max(1.0, 7.0 + gap * 1.5)
            seniority_note = (
                f"{profile.years_of_experience:g} years of experience against "
                f"{required_years:g} required."
            )

        technical_score = min(10.0, max(1.0, len(matching) * 1.5 + embedding_similarity * 4.0))
        fit_score = max(1.0, min(10.0, technical_score * 0.6 + seniority_score * 0.4))

        # Hard constraint: a posting the candidate cannot legally take should not rank.
        authorized = profile.work_authorization.is_authorized_in(job.location)
        authorization_note = ""
        if authorized is False and not job.is_remote:
            fit_score = min(fit_score, 4.0)
            authorization_note = " Location appears to fall outside the candidate's work authorization."

        reasoning = (
            f"Technical alignment {technical_score:.1f}/10 from {len(matching)} verified overlapping "
            f"skill(s) ({', '.join(matching[:4]) or 'none'}) and a Tier 1 similarity of "
            f"{embedding_similarity:.2f}. Seniority {seniority_score:.1f}/10: {seniority_note}"
            f"{authorization_note}"
        )

        return RerankerVerdict(
            fit_score=round(fit_score, 1),
            technical_score=round(technical_score, 1),
            seniority_score=round(seniority_score, 1),
            reasoning=reasoning,
            matching_skills=matching,
            missing_skills=missing[:5],
        )

    # --- Orchestration --------------------------------------------------------

    @staticmethod
    def _profile_summary(profile: CandidateProfile) -> str:
        """Condense the profile into the evidence the judge needs."""
        recent_roles = "; ".join(
            f"{exp.title} at {exp.company} ({exp.start_date} to {exp.end_date})"
            for exp in profile.experience[:3]
        )
        return (
            f"Name: {profile.contact.full_name}\n"
            f"Summary: {profile.summary}\n"
            f"Years of experience: {profile.years_of_experience:g}\n"
            f"Skills: {', '.join(profile.skills.all_skills())}\n"
            f"Lives in: {profile.work_authorization.current_country or 'unspecified'}\n"
            f"Authorized to work in: {', '.join(profile.work_authorization.authorized_countries) or 'unspecified'}\n"
            f"Needs visa sponsorship: {_sponsorship_note(profile)}\n"
            f"Open to remote work for employers in any country: {profile.work_authorization.remote_worldwide}\n"
            f"Salary expectation: {profile.salary_expectation_text() or 'unspecified'}\n"
            f"Recent roles: {recent_roles or 'none listed'}\n"
            f"Verified achievements: {'; '.join(fact.statement for fact in profile.all_locked_facts()[:6])}"
        )

    def evaluate_job(
        self,
        profile: CandidateProfile,
        job: JobPosting,
        embedding_similarity: float,
    ) -> EvaluationScore:
        """Evaluate a single posting against the candidate profile."""
        job_context = truncate(self.select_context(job.description), CONTEXT_BUDGET_CHARS)
        profile_summary = self._profile_summary(profile)

        verdict: Optional[RerankerVerdict] = None
        scored_by = "heuristic"

        if self.provider in ("openai", "anthropic", "groq", "openai_compatible"):
            caller = {"openai": self._call_openai, "anthropic": self._call_anthropic,
                      "groq": self._call_groq, "openai_compatible": self._call_openai}[self.provider]
            model = settings.model_for("rerank", self.provider)
            try:
                raw = caller(profile_summary, job, job_context)
                # Reject anything the schema will not accept rather than trusting it.
                verdict = RerankerVerdict(**raw)
                scored_by = f"{self.provider}:{model}"
            except Exception as exc:
                if settings.llm_strict:
                    raise
                console.print(
                    f"[yellow]{self.provider} re-ranker rejected or failed ({exc.__class__.__name__}: "
                    f"{str(exc)[:120]}). Falling back to the deterministic judge.[/yellow]"
                )

        if verdict is None:
            verdict = self._heuristic_reranker(profile, job, embedding_similarity)

        return EvaluationScore(
            embedding_similarity=embedding_similarity,
            fit_score=verdict.fit_score,
            technical_score=verdict.technical_score or verdict.fit_score,
            seniority_score=verdict.seniority_score or verdict.fit_score,
            threshold_used=self.min_score,
            reasoning=verdict.reasoning,
            matching_skills=verdict.matching_skills,
            missing_skills=verdict.missing_skills,
            scored_by=scored_by,
        )

    def _call_groq(self, profile_summary, job, job_context):
        from job_agent.llm import groq_complete
        return groq_complete(RERANKER_SYSTEM_PROMPT,
                             self._build_prompt(profile_summary, job, job_context), max_tokens=2048)
