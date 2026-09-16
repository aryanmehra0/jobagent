"""Resume readiness report.

Parses a resume and grades what came out, field by field, with a specific
instruction for anything missing. This is what makes the agent self-service: a
candidate whose template confuses the parser gets told which line to change,
rather than discovering later that their tailored resume omitted a job.

Nothing here modifies the profile. It is purely diagnostic, so it is safe to run
against a resume before committing to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from job_agent.config.normalize import clean_text

# Severity levels, worst first.
BLOCKER = "blocker"      # Intake cannot complete at all.
WARNING = "warning"      # Intake works, but a later phase will be degraded.
INFO = "info"            # Worth knowing; nothing is broken.

_SEVERITY_ORDER = {BLOCKER: 0, WARNING: 1, INFO: 2}


@dataclass
class Finding:
    """One observation about a resume, with the fix for it."""

    severity: str
    field: str
    detail: str
    fix: str


@dataclass
class ReadinessReport:
    """The full diagnosis of a resume."""

    document: str
    readable: bool
    characters: int = 0
    layout: str = "unknown"
    extractor: str = ""
    profile: Optional[Dict[str, Any]] = None
    findings: List[Finding] = field(default_factory=list)

    @property
    def blockers(self) -> List[Finding]:
        return [item for item in self.findings if item.severity == BLOCKER]

    @property
    def warnings(self) -> List[Finding]:
        return [item for item in self.findings if item.severity == WARNING]

    @property
    def ready(self) -> bool:
        """Whether this resume can be used without further editing."""
        return self.readable and not self.blockers

    def score(self) -> int:
        """A 0-100 readiness score, for a quick at-a-glance verdict.

        Blockers cost far more than warnings because they stop the pipeline
        outright, where a warning only degrades one later phase.
        """
        if not self.readable:
            return 0
        penalty = len(self.blockers) * 25 + len(self.warnings) * 7
        return max(0, min(100, 100 - penalty))

    def sorted_findings(self) -> List[Finding]:
        return sorted(self.findings, key=lambda item: _SEVERITY_ORDER.get(item.severity, 9))

    def to_dict(self) -> Dict[str, Any]:
        """JSON-serialisable form, used by the flow console."""
        return {
            "document": self.document,
            "readable": self.readable,
            "ready": self.ready,
            "score": self.score(),
            "characters": self.characters,
            "layout": self.layout,
            "extractor": self.extractor,
            "summary": self.summary(),
            "findings": [
                {"severity": f.severity, "field": f.field, "detail": f.detail, "fix": f.fix}
                for f in self.sorted_findings()
            ],
        }

    def summary(self) -> Dict[str, Any]:
        """What was actually extracted, for the candidate to eyeball."""
        if not self.profile:
            return {}
        contact = self.profile.get("contact", {})
        skills = self.profile.get("skills", {}) or {}
        return {
            "name": contact.get("full_name"),
            "email": contact.get("email"),
            "phone": contact.get("phone"),
            "location": contact.get("location"),
            "roles": len(self.profile.get("experience", [])),
            "education": len(self.profile.get("education", [])),
            "projects": len(self.profile.get("projects", [])),
            "certifications": len(self.profile.get("certifications", [])),
            "skills": sum(len(values) for values in skills.values()),
            "years_of_experience": self.profile.get("years_of_experience"),
            "metrics": _count_metrics(self.profile),
        }


def _count_metrics(profile: Dict[str, Any]) -> int:
    """How many quantified achievements were recognised."""
    from job_agent.intake.validator import extract_potential_metrics

    total = 0
    for role in profile.get("experience", []):
        for bullet in role.get("description_bullets", []):
            total += len(extract_potential_metrics(bullet))
    return total


def _detect_layout(document_path: Path) -> str:
    """Report whether a PDF is single- or two-column."""
    if document_path.suffix.lower() != ".pdf":
        return document_path.suffix.lstrip(".").lower() or "unknown"
    try:
        import pdfplumber

        from job_agent.intake.layout import describe_layout

        with pdfplumber.open(document_path) as pdf:
            if not pdf.pages:
                return "empty"
            return describe_layout(pdf.pages[0])[0]
    except Exception:
        return "unknown"


def check_resume(document_path: Path) -> ReadinessReport:
    """Parse a resume and report exactly what the agent could and could not read."""
    from job_agent.intake.heuristic import ResumeParseError, build_profile_dict
    from job_agent.intake.parser import ResumeParser

    report = ReadinessReport(document=document_path.name, readable=False)

    # --- Stage 1: can we get text out at all? ---
    try:
        text = ResumeParser().extract_text(document_path)
    except Exception as exc:
        report.findings.append(Finding(
            BLOCKER, "document", str(exc),
            "Export a text-based PDF (File > Export / Save as PDF), not a scan or screenshot.",
        ))
        return report

    report.readable = True
    report.characters = len(text)
    report.layout = _detect_layout(document_path)

    if report.characters < 400:
        report.findings.append(Finding(
            WARNING, "document",
            f"Only {report.characters} characters of text were found.",
            "If your resume is longer than that, its text may be inside images or "
            "text boxes. Export to PDF from the original document instead.",
        ))

    # --- Stage 2: does it parse? ---
    try:
        profile = build_profile_dict(text, source_document=document_path.name)
    except ResumeParseError as exc:
        for missing in exc.missing:
            report.findings.append(Finding(
                BLOCKER, f"contact.{missing}",
                f"Could not find your {missing}.",
                _CONTACT_FIXES.get(missing, "Add it to the top of your resume."),
            ))
        return report

    report.profile = profile
    report.extractor = profile.get("extraction_method", "deterministic")
    report.findings.extend(_grade_profile(profile, report.layout))
    return report


_CONTACT_FIXES = {
    "full_name": "Put your name on its own line at the very top, above everything else.",
    "email": "Add your email address to the header, e.g. 'you@example.com'.",
}


def _grade_profile(profile: Dict[str, Any], layout: str) -> List[Finding]:
    """Turn a parsed profile into findings about what is missing or thin."""
    findings: List[Finding] = []
    contact = profile.get("contact", {})
    experience = profile.get("experience", []) or []
    education = profile.get("education", []) or []
    skills = profile.get("skills", {}) or {}
    skill_count = sum(len(values) for values in skills.values())

    # --- Contact ---
    if not contact.get("phone"):
        findings.append(Finding(
            WARNING, "contact.phone", "No phone number found.",
            "Add it to the header with its country code, e.g. '+91 98450 11234'. "
            "Most application forms require one.",
        ))
    if not contact.get("location"):
        findings.append(Finding(
            WARNING, "contact.location", "No location found.",
            "Add 'City, Country' or 'City, ST' to the header, e.g. 'Pune, India'. "
            "Application forms ask for it, and it drives remote/onsite matching.",
        ))
    if not contact.get("linkedin_url"):
        findings.append(Finding(
            INFO, "contact.linkedin_url", "No LinkedIn URL found.",
            "Write the full address as text ('linkedin.com/in/you'), not as a "
            "hyperlink on the word 'LinkedIn' — the text is what gets read.",
        ))

    # --- Experience ---
    if not experience:
        findings.append(Finding(
            BLOCKER, "experience", "No work experience was recognised.",
            "Give each role a date range on the same line as the employer or "
            "directly beneath it, e.g. 'Acme Corp - Senior Engineer (2021 - Present)'. "
            "A date range is what marks the start of a role.",
        ))
    else:
        undated = [role for role in experience if not role.get("start_date")]
        if undated:
            findings.append(Finding(
                WARNING, "experience.dates",
                f"{len(undated)} role(s) have no start date.",
                "Add a date range like 'Mar 2021 - Present' to each role header.",
            ))

        bulletless = [role for role in experience if not role.get("description_bullets")]
        if bulletless:
            names = ", ".join(role.get("company", "?") for role in bulletless[:3])
            findings.append(Finding(
                WARNING, "experience.bullets",
                f"{len(bulletless)} role(s) have no bullet points ({names}).",
                "Start each achievement line with a bullet character so it is not "
                "mistaken for a heading.",
            ))

        if _count_metrics(profile) == 0:
            findings.append(Finding(
                WARNING, "experience.metrics",
                "No quantified achievements were found.",
                "Add numbers to your bullets ('cut latency by 45%', 'handled 200k "
                "requests/day'). These are locked as verified facts and are what "
                "stops the tailoring step from weakening your resume.",
            ))

    # --- Education, skills, summary ---
    if not education:
        findings.append(Finding(
            INFO, "education", "No education entries were recognised.",
            "Use a line like 'B.Tech in Computer Science | Your University 2018 - 2022' "
            "under an EDUCATION heading.",
        ))

    if skill_count == 0:
        findings.append(Finding(
            BLOCKER, "skills", "No skills were recognised.",
            "Add a SKILLS section with labelled lines, e.g. "
            "'Languages: Python, Go' and 'Cloud: AWS, Kubernetes'. "
            "Job matching relies on these.",
        ))
    elif skill_count < 6:
        findings.append(Finding(
            WARNING, "skills", f"Only {skill_count} skill(s) were recognised.",
            "List them comma-separated under a SKILLS heading so matching has "
            "enough to work with.",
        ))

    if len(clean_text(profile.get("summary", ""))) < 40:
        findings.append(Finding(
            INFO, "summary", "No professional summary was found.",
            "Add a two or three sentence SUMMARY at the top. It is used verbatim "
            "when tailoring your resume.",
        ))

    # --- Layout ---
    if layout == "two-column":
        findings.append(Finding(
            INFO, "layout", "This resume uses a two-column layout.",
            "It was read column by column, which works — but single-column resumes "
            "are more reliable here and with most employer ATS systems.",
        ))

    return findings
