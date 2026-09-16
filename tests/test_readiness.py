"""Tests for the resume readiness report and the multi-format reader.

The readiness report is what makes the agent usable by someone who did not write
it: instead of discovering later that a job is missing from their tailored
resume, they are told up front which line of their document to change.
"""

from pathlib import Path

import pytest

from job_agent.intake.parser import SUPPORTED_RESUME_SUFFIXES, ResumeParser
from job_agent.intake.readiness import BLOCKER, INFO, WARNING, check_resume

GOOD_RESUME = """ANITA DESAI
anita.desai@example.com | +91 98765 43210 | Pune, India
linkedin.com/in/anitadesai

PROFESSIONAL SUMMARY
Backend engineer building payment infrastructure and data pipelines.

WORK EXPERIENCE
Zeta Payments - Senior Backend Engineer (2021 - Present)
- Cut settlement latency by 43% across 2.1M daily transactions.
- Saved Rs 1.2 crore annually by consolidating three services.

Paytm - Backend Engineer (2018 - 2021)
- Built a reconciliation service handling 800k records per hour.

EDUCATION
B.Tech in Computer Science | Pune Institute of Technology 2014 - 2018

TECHNICAL SKILLS
Languages: Python, Java, Go, SQL
Cloud & DevOps: AWS, Kubernetes, Terraform, Jenkins
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


# ==============================================================================
# FORMAT SUPPORT
# ==============================================================================

def test_supported_formats_are_advertised():
    assert ".pdf" in SUPPORTED_RESUME_SUFFIXES
    assert ".docx" in SUPPORTED_RESUME_SUFFIXES


def test_plain_text_resumes_are_read(tmp_path: Path):
    path = _write(tmp_path, "resume.txt", GOOD_RESUME)
    assert "ANITA DESAI" in ResumeParser().extract_text(path)


def test_legacy_doc_format_is_refused_with_a_way_forward(tmp_path: Path):
    """.doc is a binary format python cannot read; the message must say what to do."""
    path = _write(tmp_path, "resume.doc", "irrelevant")
    with pytest.raises(ValueError, match="save as .docx or export to PDF"):
        ResumeParser().extract_text(path)


def test_unsupported_format_names_what_is_supported(tmp_path: Path):
    path = _write(tmp_path, "resume.rtf", "irrelevant")
    with pytest.raises(ValueError, match="Supported:"):
        ResumeParser().extract_text(path)


def test_docx_is_read_in_document_order(tmp_path: Path):
    """python-docx exposes paragraphs and tables as separate sequences.

    Reading one then the other moves every table to the end of the document,
    which stranded role headers after the skills section.
    """
    docx = pytest.importorskip("docx")

    path = tmp_path / "resume.docx"
    document = docx.Document()
    document.add_paragraph("WORK EXPERIENCE")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "Kotak Digital"
    table.rows[0].cells[1].text = "Mar 2021 - Present"
    document.add_paragraph("Senior Backend Engineer")
    document.add_paragraph("EDUCATION")
    document.save(str(path))

    lines = [line.strip() for line in ResumeParser().extract_text(path).splitlines() if line.strip()]
    assert lines.index("Kotak Digital  Mar 2021 - Present") < lines.index("EDUCATION")


# ==============================================================================
# READINESS REPORT
# ==============================================================================

def test_a_complete_resume_is_ready(tmp_path: Path):
    report = check_resume(_write(tmp_path, "good.txt", GOOD_RESUME))

    assert report.readable is True
    assert report.ready is True
    assert report.blockers == []
    assert report.score() >= 90

    summary = report.summary()
    assert summary["name"] == "ANITA DESAI"
    assert summary["email"] == "anita.desai@example.com"
    assert summary["roles"] == 2
    assert summary["education"] == 1
    assert summary["metrics"] >= 3, "quantified achievements must be recognised"


def test_unreadable_document_is_a_blocker(tmp_path: Path):
    path = tmp_path / "scan.pdf"
    path.write_bytes(b"not really a pdf")
    report = check_resume(path)

    assert report.readable is False
    assert report.ready is False
    assert report.score() == 0
    assert report.blockers[0].field == "document"


def test_missing_email_is_a_blocker_with_a_fix(tmp_path: Path):
    text = GOOD_RESUME.replace("anita.desai@example.com | ", "")
    report = check_resume(_write(tmp_path, "no_email.txt", text))

    assert report.ready is False
    blocker = next(item for item in report.blockers if item.field == "contact.email")
    assert "email" in blocker.fix.lower()


def test_missing_experience_is_a_blocker(tmp_path: Path):
    text = GOOD_RESUME.split("WORK EXPERIENCE")[0] + "\nTECHNICAL SKILLS\nLanguages: Python, Go\n"
    report = check_resume(_write(tmp_path, "no_exp.txt", text))

    assert any(item.field == "experience" for item in report.blockers)


def test_missing_skills_is_a_blocker(tmp_path: Path):
    text = GOOD_RESUME.split("TECHNICAL SKILLS")[0]
    report = check_resume(_write(tmp_path, "no_skills.txt", text))

    assert any(item.field == "skills" for item in report.blockers)


def test_missing_phone_and_location_are_warnings_not_blockers(tmp_path: Path):
    text = GOOD_RESUME.replace(" | +91 98765 43210 | Pune, India", "")
    report = check_resume(_write(tmp_path, "thin_contact.txt", text))

    fields = {item.field for item in report.warnings}
    assert "contact.phone" in fields
    assert "contact.location" in fields
    assert report.blockers == [], "these degrade later phases but do not stop intake"


def test_absent_metrics_are_flagged(tmp_path: Path):
    text = GOOD_RESUME.replace("by 43% across 2.1M daily transactions", "noticeably")
    text = text.replace("Rs 1.2 crore annually", "money")
    text = text.replace("800k records per hour", "many records")
    report = check_resume(_write(tmp_path, "no_metrics.txt", text))

    finding = next(item for item in report.findings if item.field == "experience.metrics")
    assert finding.severity == WARNING
    assert "numbers" in finding.fix.lower()


def test_every_finding_carries_an_actionable_fix(tmp_path: Path):
    """A diagnosis without an instruction is not useful to a non-author."""
    report = check_resume(_write(tmp_path, "sparse.txt", "Someone\nsomeone@example.com\n"))

    assert report.findings
    for item in report.findings:
        assert item.severity in {BLOCKER, WARNING, INFO}
        assert item.fix and len(item.fix) > 20
        assert item.detail


def test_report_is_json_serialisable(tmp_path: Path):
    """The flow console renders this over the wire."""
    import json

    report = check_resume(_write(tmp_path, "good.txt", GOOD_RESUME))
    payload = json.loads(json.dumps(report.to_dict()))

    assert payload["ready"] is True
    assert payload["summary"]["name"] == "ANITA DESAI"
    assert isinstance(payload["findings"], list)


def test_checking_a_resume_writes_nothing(tmp_path: Path, monkeypatch):
    """`check` is diagnostic; it must not touch the sealed profile."""
    from job_agent.config.settings import settings as live_settings

    profile_path = tmp_path / "profile.json"
    monkeypatch.setattr(live_settings, "profile_path", profile_path)

    check_resume(_write(tmp_path, "good.txt", GOOD_RESUME))
    assert not profile_path.exists()
