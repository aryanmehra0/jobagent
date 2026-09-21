"""Tailoring the candidate's own PDF: complete, exact, reordered per job, and validated."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import fitz
import pytest

from job_agent.config.schema import JobPosting
from job_agent.tailoring import faithful

BLUE = (0.12, 0.22, 0.39)


def _resume(path: Path, *, sidebar: bool = False, bullets: dict | None = None) -> Path:
    """A one-page resume in the common layout: headings, roles with dates, bullets, links."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 40

    def text(x, value, size=10, bold=False, color=(0, 0, 0)):
        page.insert_text((x, y), value, fontname="hebo" if bold else "helv", fontsize=size, color=color)

    text(230, "ASHA VERMA", size=17, bold=True, color=BLUE)
    y += 16
    text(180, "asha@example.org  |  LinkedIn  |  GitHub")
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(270, y - 10, 310, y + 2),
                      "uri": "https://www.linkedin.com/in/asha"})
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(318, y - 10, 352, y + 2),
                      "uri": "https://github.com/asha"})
    y += 22

    roles = bullets or {
        "Product Manager - Finbank  |  Lending, Mumbai": [
            ("Growth:", "Ran pricing experiments across 3 markets and grew", "revenue by 12% in two quarters."),
            ("Machine learning:", "Shipped a credit-risk model with the data science", "team that cut defaults by 9%."),
            ("Operations:", "Automated onboarding checks for 40 partner banks and", "halved manual review time."),
        ],
    }
    text(34, "PROFESSIONAL EXPERIENCE", size=10.5, bold=True, color=BLUE)
    page.draw_line((34, y + 3), (560, y + 3), color=BLUE, width=0.8)
    y += 17
    for header, items in roles.items():
        text(34, header, bold=True)
        text(470, "Jan 2023 - Present", bold=True)
        y += 12
        for lead, first, second in items:
            page.insert_text((37, y), "•", fontname="tiro", fontsize=10)
            text(47, f"{lead} {first}")
            y += 12
            text(47, second)
            y += 12
        y += 3

    text(34, "PROJECTS", size=10.5, bold=True, color=BLUE)
    page.draw_line((34, y + 3), (560, y + 3), color=BLUE, width=0.8)
    y += 17
    for title, line1, line2 in (
        ("Budget Planner App  |  React, Firebase", "Built a budgeting app used by 2,000 students to", "track monthly spending."),
        ("LLM Support Agent  |  Python, RAG, FAISS", "Built a retrieval augmented generation agent that", "answers support tickets with 91% accuracy."),
    ):
        text(34, title, bold=True)
        y += 12
        page.insert_text((37, y), "•", fontname="tiro", fontsize=10)
        text(47, line1)
        y += 12
        text(47, line2)
        y += 15

    text(34, "SKILLS", size=10.5, bold=True, color=BLUE)
    page.draw_line((34, y + 3), (560, y + 3), color=BLUE, width=0.8)
    y += 17
    for line in ("Product: Roadmapping, A/B testing, stakeholder management",
                 "AI/ML: PyTorch, RAG, FAISS, LLM evaluation",
                 "Data: SQL, Python, dbt, Looker"):
        page.insert_text((37, y), "•", fontname="tiro", fontsize=10)
        text(47, line)
        y += 12

    if sidebar:
        # A second column beside the experience section.
        page.insert_text((470, 150), "Languages", fontname="helv", fontsize=9)
    doc.save(str(path))
    return path


def _job(title: str, description: str) -> JobPosting:
    return JobPosting(id="j" + str(abs(hash(title)) % 10**8), title=title, company="Acme",
                      job_url="https://acme.example/jobs/1", source="lever", description=description)


AI_JOB = ("Machine Learning Engineer",
          "Build RAG pipelines with LLM agents in Python, PyTorch and FAISS. Own model evaluation "
          "and machine learning credit-risk models with the data science team.")
PM_JOB = ("Associate Product Manager",
          "Own the roadmap, run pricing experiments and A/B testing, grow revenue, work with "
          "stakeholder management across markets.")


def _words(path: Path) -> Counter:
    doc = fitz.open(str(path))
    words = Counter(w[4] for page in doc for w in page.get_text("words"))
    doc.close()
    return words


# ==============================================================================
# READING THE LAYOUT
# ==============================================================================

def test_layout_keeps_wrapped_lines_inside_their_bullet(tmp_path):
    layout = faithful.read_layout(_resume(tmp_path / "r.pdf"))
    titles = [section.title for section in layout.sections]
    assert titles[-3:] == ["PROFESSIONAL EXPERIENCE", "PROJECTS", "SKILLS"]

    role = layout.sections[-3].entries[0]
    assert role.title.startswith("Product Manager - Finbank")
    assert [len(bullet.lines) for bullet in role.bullets] == [2, 2, 2]
    assert role.bullets[1].text == ("Machine learning: Shipped a credit-risk model with the data science "
                                   "team that cut defaults by 9%.")
    assert [len(entry.bullets) for entry in layout.sections[-2].entries] == [1, 1]
    assert len(layout.sections[-1].entries[0].bullets) == 3


# ==============================================================================
# TAILORING
# ==============================================================================

@pytest.mark.parametrize("title,description", [AI_JOB, PM_JOB])
def test_tailored_pdf_keeps_every_word_link_and_page_and_passes_all_checks(tmp_path, title, description):
    source = _resume(tmp_path / "r.pdf")
    result = faithful.tailor_pdf(source, _job(title, description), tmp_path / "out.pdf")

    assert result.validation["passed"], result.validation["checks"]
    assert _words(source) == _words(result.pdf_path)
    source_links = sorted(l["uri"] for l in fitz.open(str(source))[0].get_links())
    output_links = sorted(l["uri"] for l in fitz.open(str(result.pdf_path))[0].get_links())
    assert source_links == output_links
    assert len(fitz.open(str(result.pdf_path))) == 1


def test_each_job_gets_its_own_order(tmp_path):
    source = _resume(tmp_path / "r.pdf")
    ai = faithful.tailor_pdf(source, _job(*AI_JOB), tmp_path / "ai.pdf")
    pm = faithful.tailor_pdf(source, _job(*PM_JOB), tmp_path / "pm.pdf")
    assert ai.tailored and ai.validation["passed"]

    def reading(path):
        return " ".join(page.get_text("text", sort=True) for page in fitz.open(str(path)))

    ai_text = reading(ai.pdf_path)
    # The AI role leads with the machine learning bullet, the LLM project and the AI skills.
    assert ai_text.index("Machine learning:") < ai_text.index("Growth:")
    assert ai_text.index("LLM Support Agent") < ai_text.index("Budget Planner App")
    assert ai_text.index("AI/ML:") < ai_text.index("Product: Roadmapping")
    # The product role keeps growth first and product skills first.
    pm_text = reading(pm.pdf_path)
    assert pm_text.index("Growth:") < pm_text.index("Machine learning:")
    assert pm_text.index("Product: Roadmapping") < pm_text.index("AI/ML:")


def test_roles_stay_in_date_order_and_nothing_is_reworded(tmp_path):
    source = _resume(tmp_path / "r.pdf", bullets={
        "Product Manager - Finbank": [("Growth:", "Grew revenue by 12%", "in two quarters.")],
        "Analyst - Tradeco": [("Machine learning:", "Built a RAG model with LLM agents", "in PyTorch.")],
    })
    result = faithful.tailor_pdf(source, _job(*AI_JOB), tmp_path / "out.pdf")
    text = " ".join(page.get_text("text", sort=True) for page in fitz.open(str(result.pdf_path)))
    assert text.index("Finbank") < text.index("Tradeco")
    assert _words(source) == _words(result.pdf_path)


def test_ats_reading_order_follows_the_page_not_the_drawing_order(tmp_path):
    """Moved bullets must be read under their own role, not after the last section."""
    from pypdf import PdfReader

    source = _resume(tmp_path / "r.pdf")
    result = faithful.tailor_pdf(source, _job(*AI_JOB), tmp_path / "out.pdf")
    assert result.tailored
    stream_text = " ".join(page.extract_text() for page in PdfReader(str(result.pdf_path)).pages)
    assert stream_text.index("Machine learning:") < stream_text.index("PROJECTS")
    assert stream_text.index("AI/ML:") > stream_text.index("SKILLS")
    for name in ("reading_order_pypdf", "reading_order_mupdf"):
        assert result.validation["checks"][name]["passed"]


# ==============================================================================
# SAFETY NETS
# ==============================================================================

def test_validation_rejects_a_pdf_whose_text_is_read_out_of_order(tmp_path):
    source = _resume(tmp_path / "r.pdf")
    layout = faithful.read_layout(source)
    plan = faithful.plan_for_job(layout, _job(*AI_JOB))
    assert plan.moving

    # The page drawn with its moved blocks last: the defect the reading-order check exists for.
    faithful.build(layout, plan, tmp_path / "good.pdf")
    doc = fitz.open(str(source))
    bad = fitz.open()
    page = bad.new_page(width=doc[0].rect.width, height=doc[0].rect.height)
    page.show_pdf_page(page.rect, faithful._background(source, 0, []), 0)
    skills = next(g for g in plan.moving if "Skill" in g.description)
    for strip in (fitz.Rect(0, skills.region.y1, page.rect.width, page.rect.height),
                  fitz.Rect(0, 0, page.rect.width, skills.region.y0)):
        page.show_pdf_page(strip, faithful._isolated(source, 0, strip, page.rect, False), 0, clip=strip)
    for unit in skills.units:
        page.show_pdf_page(unit.band, faithful._isolated(source, 0, unit.band, page.rect, True), 0, clip=unit.band)
    bad.save(str(tmp_path / "bad.pdf"))

    report = faithful.validate(layout, plan, tmp_path / "bad.pdf")
    assert not report["passed"]
    assert not report["checks"]["reading_order_pypdf"]["passed"]


def test_a_failed_check_falls_back_to_the_exact_original(tmp_path, monkeypatch):
    source = _resume(tmp_path / "r.pdf")
    real_validate = faithful.validate
    calls = {"n": 0}

    def failing_first(layout, plan, output):
        calls["n"] += 1
        report = real_validate(layout, plan, output)
        if calls["n"] == 1:
            report["passed"] = False
            report["checks"]["words_pypdf"] = {"passed": False, "detail": "simulated"}
        return report

    monkeypatch.setattr(faithful, "validate", failing_first)
    result = faithful.tailor_pdf(source, _job(*AI_JOB), tmp_path / "out.pdf")
    assert not result.tailored
    assert result.pdf_path.read_bytes() == source.read_bytes()
    assert result.validation["passed"] and "rejected_checks" in result.validation
    assert "original resume is used unchanged" in result.notes[-1]


def test_a_second_column_beside_a_section_is_never_moved(tmp_path):
    source = _resume(tmp_path / "r.pdf", sidebar=True)
    result = faithful.tailor_pdf(source, _job(*AI_JOB), tmp_path / "out.pdf")
    assert result.validation["passed"]
    assert any("column layout" in note for note in result.notes)
    assert _words(source) == _words(result.pdf_path)


def test_summary_line_for_the_spreadsheet(tmp_path):
    source = _resume(tmp_path / "r.pdf")
    result = faithful.tailor_pdf(source, _job(*AI_JOB), tmp_path / "out.pdf")
    line = faithful.summarize(result)
    assert line.startswith("PASS ") and "checks" in line and "reordered" in line


# ==============================================================================
# PIPELINE
# ==============================================================================

def test_pipeline_tailors_the_uploaded_pdf_and_records_the_validation(tmp_path, monkeypatch):
    from job_agent.config.schema import CandidateProfile, ContactInfo, SkillSet, WorkAuthorization
    from job_agent.config.settings import settings
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tailoring.compiler import TypstResumeCompiler
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    raw = tmp_path / "raw"
    raw.mkdir()
    _resume(raw / "asha.pdf")
    monkeypatch.setattr(settings, "raw_resumes_dir", raw)
    monkeypatch.setattr(settings, "tailoring_mode", "auto")

    profile = CandidateProfile(
        contact=ContactInfo(full_name="Asha Verma", email="asha@example.org"),
        summary="Product manager with lending and machine learning experience.",
        work_authorization=WorkAuthorization(), skills=SkillSet(languages=["Python"]),
        years_of_experience=3.0, source_document="asha.pdf",
    ).seal_profile()
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")

    job = _job(*AI_JOB)
    qualified = tmp_path / "qualified_jobs.json"
    qualified.write_text(json.dumps([{"job": job.model_dump(), "evaluation": {
        "embedding_similarity": 0.6, "fit_score": 8.0, "technical_score": 8.0, "seniority_score": 8.0,
        "threshold_used": 7.0, "passed_threshold": True, "reasoning": "Strong match for the role.",
        "matching_skills": [], "missing_skills": [], "scored_by": "test"}}]), encoding="utf-8")

    pipeline = ResumeTailoringPipeline(compiler=TypstResumeCompiler(output_dir=tmp_path / "tailored"),
                                       delta_store=DeltaStore(tmp_path / "d.db"))
    records = pipeline.run_tailoring(profile_path=profile_path, qualified_jobs_path=qualified)

    assert len(records) == 1
    record = records[0]
    assert record["mode"] == "faithful" and record["validation_passed"] is True
    assert record["validation_summary"].startswith("PASS")
    assert Path(record["pdf_path"]).is_file()
    assert _words(raw / "asha.pdf") == _words(Path(record["pdf_path"]))


# ==============================================================================
# NEVER A DUMMY RESUME
# ==============================================================================

def _profile(name: str, source: str):
    from job_agent.config.schema import CandidateProfile, ContactInfo, SkillSet, WorkAuthorization

    return CandidateProfile(
        contact=ContactInfo(full_name=name, email="asha@example.org"),
        summary="Product manager with lending and machine learning experience.",
        work_authorization=WorkAuthorization(), skills=SkillSet(languages=["Python"]),
        years_of_experience=3.0, source_document=source,
    ).seal_profile()


def test_the_real_resume_is_chosen_over_the_demo_whatever_its_name(tmp_path):
    import os
    import time

    from job_agent.intake.preferences import choose_resume

    folder = tmp_path / "raw"
    folder.mkdir()
    (folder / "sample_resume.pdf").write_bytes(b"%PDF demo")
    time.sleep(0.01)
    (folder / "zara_resume.pdf").write_bytes(b"%PDF real")   # sorts after "sample_resume.pdf"
    assert choose_resume(folder).name == "zara_resume.pdf"

    (folder / "zara_resume.pdf").unlink()
    assert choose_resume(folder).name == "sample_resume.pdf"   # the demo only when nothing else


def test_tailoring_refuses_a_demo_profile_when_a_real_resume_is_uploaded(tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tailoring.compiler import TypstResumeCompiler
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    raw = tmp_path / "raw"
    raw.mkdir()
    _resume(raw / "sample_resume.pdf")
    _resume(raw / "asha.pdf")
    monkeypatch.setattr(settings, "raw_resumes_dir", raw)
    pipeline = ResumeTailoringPipeline(compiler=TypstResumeCompiler(output_dir=tmp_path / "t"),
                                       delta_store=DeltaStore(tmp_path / "d.db"))
    with pytest.raises(ValueError, match="demo resume"):
        pipeline._faithful_source(_profile("Alex Rivera", "sample_resume.pdf"))


def test_an_error_while_reordering_yields_the_original_never_a_generated_resume(tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tailoring.compiler import TypstResumeCompiler
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    raw = tmp_path / "raw"
    raw.mkdir()
    source = _resume(raw / "asha.pdf")
    monkeypatch.setattr(settings, "raw_resumes_dir", raw)
    monkeypatch.setattr(faithful, "tailor_pdf", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    pipeline = ResumeTailoringPipeline(compiler=TypstResumeCompiler(output_dir=tmp_path / "t"),
                                       delta_store=DeltaStore(tmp_path / "d.db"))

    record = pipeline._tailor_faithfully(source, _profile("Asha Verma", "asha.pdf"), _job(*AI_JOB), 8.0)
    assert record is not None and record.mode == "faithful"
    assert Path(record.pdf_path).read_bytes() == source.read_bytes()
    assert record.validation_passed


def test_resumes_of_another_person_are_moved_out_of_the_folder(tmp_path, monkeypatch):
    from job_agent.config.settings import settings
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tailoring.compiler import TypstResumeCompiler
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    raw = tmp_path / "raw"
    raw.mkdir()
    source = _resume(raw / "asha.pdf")
    monkeypatch.setattr(settings, "raw_resumes_dir", raw)
    out = tmp_path / "t"
    out.mkdir()
    (out / "resume_mine.pdf").write_bytes(source.read_bytes())
    demo = fitz.open()
    demo.new_page().insert_text((72, 72), "ALEX RIVERA  Senior Cloud Engineer")
    demo.save(str(out / "resume_demo.pdf"))

    pipeline = ResumeTailoringPipeline(compiler=TypstResumeCompiler(output_dir=out),
                                       delta_store=DeltaStore(tmp_path / "d.db"))
    moved = pipeline._archive_foreign_resumes(_profile("Asha Verma", "asha.pdf"))
    assert moved == ["resume_demo.pdf"]
    assert (out / "resume_mine.pdf").is_file() and not (out / "resume_demo.pdf").exists()
    assert list((settings.outputs_dir / "history").glob("*_foreign_resumes/resume_demo.pdf"))


def test_the_database_keeps_the_resume_pdf_and_it_exports_byte_for_byte(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from job_agent.cli import cli
    from job_agent.config.settings import settings
    from job_agent.storage.jobs_db import JobsDatabase

    out = tmp_path / "outputs"
    (out / "tailored_resumes").mkdir(parents=True)
    monkeypatch.setattr(settings, "outputs_dir", out)
    job = _job(*AI_JOB)
    (out / "scraped_jobs.json").write_text(json.dumps([job.model_dump()]), encoding="utf-8")
    pdf = _resume(out / "tailored_resumes" / f"resume_{job.id}.pdf")
    (out / "tailored_resumes" / "manifest.json").write_text(json.dumps([{
        "job_id": job.id, "pdf_path": str(pdf), "validation_summary": "PASS 10/10 checks"}]), encoding="utf-8")

    database = JobsDatabase()
    stats = database.sync(out)
    assert stats["resumes"] == 1
    assert database.sync(out)["resumes"] == 0          # unchanged files are not rewritten
    row = database.jobs()[0]
    assert row["resume_file"].endswith(pdf.name) and row["resume_bytes"] == pdf.stat().st_size

    target = tmp_path / "attach" / "resume.pdf"
    result = CliRunner().invoke(cli, ["db", "resume", job.id, "--out", str(target)])
    assert result.exit_code == 0, result.output
    assert target.read_bytes() == pdf.read_bytes()


def test_the_csv_has_a_one_click_link_to_each_resume(tmp_path):
    import csv

    from job_agent.tracking.export import JobsCsvExporter

    out = tmp_path / "out"
    (out / "tailored_resumes").mkdir(parents=True)
    job = _job(*AI_JOB)
    (out / "scraped_jobs.json").write_text(json.dumps([job.model_dump()]), encoding="utf-8")
    pdf = _resume(out / "tailored_resumes" / f"resume_{job.id}.pdf")
    (out / "tailored_resumes" / "manifest.json").write_text(json.dumps(
        [{"job_id": job.id, "pdf_path": str(pdf)}]), encoding="utf-8")

    exporter = JobsCsvExporter(outputs_dir=out)
    exporter.export()
    with exporter.csv_path.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["Open Resume"] == f'=HYPERLINK("{pdf.resolve()}","Open {pdf.name}")'
