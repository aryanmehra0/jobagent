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
        client = OpenAI(api_key=settings.openai_api_key)

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
            model=settings.llm_tailor_model,
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

    def _heuristic_email(self, profile: CandidateProfile, job: JobPosting) -> str:
        """Deterministic outreach template built only from verified candidate facts.

        Every highlight comes from a locked fact or a listed skill. Where the
        profile has nothing to offer, the line is omitted rather than filled with a
        generic claim ("impressed by your work", "high-availability environments")
        that the candidate never made and cannot defend in an interview.
        """
        highlights: List[str] = []

        locked = profile.all_locked_facts()
        if locked:
            highlights.extend(fact.statement for fact in locked[:2])
        elif profile.experience and profile.experience[0].description_bullets:
            highlights.append(profile.experience[0].description_bullets[0])

        top_skills = profile.skills.all_skills()[:6]
        if top_skills:
            highlights.append("Core stack: " + ", ".join(top_skills))
        if profile.years_of_experience:
            highlights.append(f"{profile.years_of_experience:g} years of professional experience")

        contact_line = " | ".join(
            part for part in (profile.contact.email, profile.contact.phone) if part
        )

        lines = [
            f"Subject: {job.title} — {profile.contact.full_name}",
            "",
            f"Hi {job.company} Hiring Team,",
            "",
            f"I came across the {job.title} role at {job.company} and wanted to reach out directly.",
            "",
        ]
        if highlights:
            lines.append("A few relevant highlights from my background:")
            lines.extend(f"• {item}" for item in highlights)
            lines.append("")
        lines.extend([
            "My tailored resume is attached. If you are open to it, I would welcome a brief "
            "10-15 minute conversation about how I could contribute.",
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
        if self.provider == "openai":
            try:
                return self._call_openai(profile, job, fit_score)
            except Exception as e:
                console.print(f"[yellow]OpenAI email generation note: {e}. Using deterministic synthesis.[/yellow]")
        elif self.provider == "anthropic":
            try:
                return self._call_anthropic(profile, job, fit_score)
            except Exception as e:
                console.print(f"[yellow]Anthropic email generation note: {e}. Using deterministic synthesis.[/yellow]")

        return self._heuristic_email(profile, job)

