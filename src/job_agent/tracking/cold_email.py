"""Personalized Cold Outreach Email Synthesis Engine.

Generates targeted, high-impact cold emails to hiring managers and recruiters
cross-referencing candidate verified locked facts with specific job requirements.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from rich.console import Console

from job_agent.config.settings import settings
from job_agent.config.schema import CandidateProfile, JobPosting

console = Console()

COLD_EMAIL_SYSTEM_PROMPT = """You are an elite executive talent agent and communications specialist.
Your mission is to draft a hyper-personalized, concise cold outreach email from the candidate to the hiring manager or engineering leader for this role.

CRITICAL CONSTRAINTS:
1. BREVITY: Keep the email under 200 words. Busy hiring managers read on mobile.
2. FACTUAL HONESTY: Only cite metrics, scale figures, and achievements that exist in the candidate's profile.
3. SPECIFIC VALUE PROPOSITION: Explicitly connect candidate's locked achievements to the company's tech stack or domain challenges.
4. TONE: Confident, professional, collaborative, and human. No generic fluff or robotic phrases.

Output format:
Subject: [Compelling subject line]

Hi [Team / Hiring Manager],

[Paragraph 1: Direct hook referencing the specific role and company mission]

[Paragraph 2: 2-3 concise bullet points with verified candidate metrics directly aligning with their tech requirements]

[Paragraph 3: Low-friction call to action proposing a brief 15-minute sync]

Best regards,
[Candidate Full Name]
"""


class ColdEmailGenerator:
    """Generates customized cold outreach emails for fallback tracking."""

    def __init__(self, provider: Optional[str] = None):
        self.provider = (provider or settings.active_provider).lower()

    def _call_openai(self, profile: CandidateProfile, job: JobPosting, fit_score: float) -> str:
        """Synthesize cold email via OpenAI."""
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

        prompt = (
            f"=== TARGET JOB ===\n"
            f"Role: {job.title}\n"
            f"Company: {job.company}\n"
            f"URL: {job.job_url}\n"
            f"Description: {job.description[:2000]}\n"
            f"Match Score: {fit_score}/10.0\n\n"
            f"=== CANDIDATE PROFILE ===\n"
            f"Name: {profile.contact.full_name}\n"
            f"Summary: {profile.summary}\n"
            f"Experience Years: {profile.years_of_experience}\n"
            f"Skills: {', '.join(profile.skills.languages + profile.skills.cloud_devops + profile.skills.developer_tools)}\n"
            f"Recent Role Achievements:\n"
            + "\n".join(f"- {b}" for b in (profile.experience[0].description_bullets if profile.experience else []))
        )

        response = client.chat.completions.create(
            model=model,
            temperature=0.3,
            messages=[
                {"role": "system", "content": COLD_EMAIL_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        return response.choices[0].message.content.strip()

    def _call_anthropic(self, profile: CandidateProfile, job: JobPosting, fit_score: float) -> str:
        """Synthesize cold email via Anthropic Claude."""
        import anthropic
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

        prompt = (
            f"=== TARGET JOB ===\n"
            f"Role: {job.title} at {job.company}\n"
            f"Description: {job.description[:2000]}\n\n"
            f"=== CANDIDATE PROFILE ===\n"
            f"Name: {profile.contact.full_name}\n"
            f"Summary: {profile.summary}\n"
            f"Skills: {', '.join(profile.skills.languages + profile.skills.cloud_devops)}\n\n"
            f"Draft the cold email following the system guidelines:"
        )

        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=1000,
            temperature=0.3,
            system=COLD_EMAIL_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()

    def _heuristic_email(self, profile: CandidateProfile, job: JobPosting, selected=None) -> str:
        """Deterministic outreach template built only from verified candidate facts.

        Every highlight comes from a locked fact or a listed skill. Where the
        profile has nothing to offer, the line is omitted rather than filled with a
        generic claim ("impressed by your work", "high-availability environments")
        that the candidate never made and cannot defend in an interview.
        """
        from job_agent.config.normalize import dedupe_preserving_order
        from job_agent.tailoring.rewriter import job_terms, relevance, skill_in_posting

        evidence = dedupe_preserving_order(
            [fact.statement for fact in profile.all_locked_facts()]
            + [bullet for role in profile.experience for bullet in role.description_bullets]
        )
        ranked = sorted(
            enumerate(evidence),
            key=lambda item: (-relevance(item[1], job_terms(job)), item[0]),
        )
        highlights = list(selected or [item for _, item in ranked[:3]])

        matching_skills = [
            skill for skill in profile.skills.all_skills() if skill_in_posting(skill, job)
        ][:5]

        contact_line = " | ".join(
            part for part in (profile.contact.email, profile.contact.phone) if part
        )

        lines = [
            f"Subject: Application for {job.title} at {job.company} | {profile.contact.full_name}",
            "",
            f"Hi {job.company} Hiring Team,",
            "",
            f"I am applying for the {job.title} role at {job.company}. My background aligns with the role"
            + (f" through {', '.join(matching_skills)}." if matching_skills else "."),
            "",
        ]
        if highlights:
            lines.append("Relevant evidence from my resume:")
            lines.extend(f"• {item}" for item in highlights)
            lines.append("")
        lines.extend([
            "I have attached an ATS-friendly resume tailored to the posted requirements. "
            "Would you be open to a 15-minute conversation about the team's priorities for this role?",
            "",
            "Best regards,",
            profile.contact.full_name,
        ])
        if contact_line:
            lines.append(contact_line)
        if profile.contact.linkedin_url:
            lines.append(profile.contact.linkedin_url)

        return "\n".join(lines)

    def generate_email(self, profile: CandidateProfile, job: JobPosting, fit_score: float = 8.0) -> str:
        """Generate a personalized cold outreach email for the target job."""
        if self.provider == "groq":
            from job_agent.llm import groq_complete
            try:
                evidence = [bullet for role in profile.experience for bullet in role.description_bullets]
                selection = groq_complete(
                    'Select up to three relevant candidate achievements for this job. Return JSON {"indices": [0, 1]}. Use only indices from the supplied evidence.',
                    f"Evidence: {json.dumps(list(enumerate(evidence)))}\n"
                    f"Target role: {job.title} at {job.company}\n{job.description[:3000]}",
                    max_tokens=2048,
                )
                indices = selection.get("indices", [])
                if not isinstance(indices, list) or any(type(i) is not int or not 0 <= i < len(evidence) for i in indices):
                    raise ValueError("Groq returned invalid outreach evidence indices.")
                chosen = [evidence[i] for i in dict.fromkeys(indices)][:3]
                return self._heuristic_email(profile, job, selected=chosen)
            except Exception:
                if settings.llm_strict:
                    raise
                console.print("[yellow]Groq outreach unavailable; using verified template.[/yellow]")
        if self.provider in ("openai", "openai_compatible"):
            try:
                return self._call_openai(profile, job, fit_score)
            except Exception as e:
                console.print(f"[yellow]{self.provider} email generation note: {e}. Using deterministic synthesis.[/yellow]")
        elif self.provider == "anthropic":
            try:
                return self._call_anthropic(profile, job, fit_score)
            except Exception as e:
                console.print(f"[yellow]Anthropic email generation note: {e}. Using deterministic synthesis.[/yellow]")

        return self._heuristic_email(profile, job)

