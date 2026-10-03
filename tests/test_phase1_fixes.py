"""Phase 1 of the production audit: things the product reported wrongly or did silently.

C1  readiness / funnel counted a resume as a qualification
C2  a judge score of 10.5 was stored as 1.05
C5  one candidate's runs were shown to the next
C6  a failed profile switch reported success
H4  the dashboard described tailoring as something it no longer does
H7  run history and the profile switch disagreed about who a candidate is
"""
from __future__ import annotations

import json
import sqlite3
import threading
from functools import partial
from urllib.request import urlopen

import pytest

from job_agent.config.settings import settings
from job_agent.intake import switch
from job_agent.storage.jobs_db import JobsDatabase
from job_agent.web.server import FlowConsoleHandler, FlowConsoleServer
from tests.test_tailoring import candidate_profile  # noqa: F401  (fixture)


# ------------------------------------------------------------------ C2 ----------

@pytest.mark.parametrize("raw,expected", [(10.5, 10.0), (11.0, 10.0), (12.0, 10.0), (9.9, 9.9), (7.5, 7.5),
                                          (95.0, 9.5), (100.0, 10.0)])
def test_a_score_a_little_over_ten_is_a_perfect_score_not_a_terrible_one(raw, expected):
    from job_agent.config.schema import RerankerVerdict

    verdict = RerankerVerdict(fit_score=raw, technical_score=raw, seniority_score=raw, reasoning="r",
                              matching_skills=[], missing_skills=[])
    assert verdict.fit_score == pytest.approx(expected)


def test_a_legitimate_zero_sub_score_is_not_replaced_by_the_fit_score(monkeypatch, candidate_profile):
    from job_agent.config.schema import JobPosting
    from job_agent.evaluation.reranker import LLMReranker

    reranker = LLMReranker(provider="groq")
    monkeypatch.setattr(reranker, "_call_groq", lambda *a, **k: {
        "fit_score": 6.0, "technical_score": 0.0, "seniority_score": 0.0, "reasoning": "no overlap at all",
        "matching_skills": [], "missing_skills": []})
    job = JobPosting(id="z1", title="Chef", company="Acme", job_url="https://a.example/j", source="indeed",
                     description="Cook meals for a restaurant kitchen.")
    score = reranker.evaluate_job(candidate_profile, job, 0.1)
    assert score.technical_score == 0.0 and score.seniority_score == 0.0


# ------------------------------------------------------------------ C6 ----------

def test_a_failed_switch_says_so_instead_of_reporting_success(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(settings, "outputs_dir", out)
    monkeypatch.setattr(settings, "profile_path", tmp_path / "profiles" / "profile.json")
    JobsDatabase()

    class Locked(Exception):
        pass

    real = JobsDatabase._connect

    def locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(JobsDatabase, "_connect", locked)
    result = switch.retire_previous_candidate()
    monkeypatch.setattr(JobsDatabase, "_connect", real)

    assert result["complete"] is False
    assert any("locked" in problem for problem in result["problems"]), result
    # It must not claim the previous candidate's rows were cleared.
    assert result["database"] == {}


def test_nothing_is_cleared_when_the_backup_could_not_be_made(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(settings, "outputs_dir", out)
    monkeypatch.setattr(settings, "profile_path", tmp_path / "profiles" / "profile.json")
    database = JobsDatabase()
    with database._connect() as conn:
        conn.execute(database._sql(
            "INSERT INTO jobs (job_id, title, company, status, fit_score, updated_at) "
            "VALUES ('j1', 'AI Engineer', 'Acme', 'applied', 8.5, '2026-10-01')"))

    def no_backup(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(switch, "_open_backup_file", no_backup)
    result = switch.retire_previous_candidate()

    assert result["complete"] is False and any("backup" in problem.lower() for problem in result["problems"])
    assert result["database"] == {}, "without a backup the previous candidate's rows must be left alone"
    with database._connect() as conn:
        assert conn.execute("SELECT fit_score FROM jobs WHERE job_id = 'j1'").fetchone()[0] == 8.5


def test_a_clean_switch_reports_complete(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(settings, "outputs_dir", out)
    monkeypatch.setattr(settings, "profile_path", tmp_path / "profiles" / "profile.json")
    JobsDatabase()
    result = switch.retire_previous_candidate()
    assert result["complete"] is True and result["problems"] == []


# ------------------------------------------------------------------ C5 / H7 -----

def _serve():
    server = FlowConsoleServer(("127.0.0.1", 0), partial(FlowConsoleHandler), "t")
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _get_json(server, path):
    with urlopen(f"http://127.0.0.1:{server.server_address[1]}{path}", timeout=10) as response:
        return json.loads(response.read())


def test_run_history_is_scoped_to_the_candidate_by_a_single_identity_key(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(settings, "outputs_dir", out)
    profile_file = tmp_path / "profiles" / "profile.json"
    profile_file.parent.mkdir()
    monkeypatch.setattr(settings, "profile_path", profile_file)

    database = JobsDatabase()
    for run_id, name, key in (("a1", "Asha Verma", "asha@example.org"), ("a2", "Asha Verma", "asha@example.org"),
                              ("r1", "Ravi Rao", "ravi@example.org")):
        database.save_run({"run_id": run_id, "started_at": f"2026-10-0{1 + len(run_id)}T09:00:00", "status": "ok",
                           "candidate_name": name, "candidate_key": key, "results": {}})
    # Two people can share a name; the key tells them apart.
    database.save_run({"run_id": "a3", "started_at": "2026-10-09T09:00:00", "status": "ok",
                       "candidate_name": "Asha Verma", "candidate_key": "asha.other@example.org", "results": {}})

    profile_file.write_text(json.dumps({"contact": {"full_name": "Ravi Rao", "email": "Ravi@Example.org"}}))
    server = _serve()
    try:
        everyone = _get_json(server, "/api/runs")
        mine = _get_json(server, "/api/runs?profile=ravi@example.org")
        asha = _get_json(server, "/api/runs?profile=asha@example.org")
    finally:
        server.shutdown()

    assert everyone["current_profile"] == "ravi@example.org"
    assert {r["run_id"] for r in mine["runs"]} == {"r1"}
    assert {r["run_id"] for r in asha["runs"]} == {"a1", "a2"}, "a namesake with a different email is not Asha"
    assert {p["key"] for p in everyone["profiles"]} == {"asha@example.org", "ravi@example.org", "asha.other@example.org"}


def test_a_new_run_records_the_same_key_the_profile_switch_uses(monkeypatch, tmp_path):
    from job_agent import workflow
    from job_agent.sourcing.delta_store import profile_identity
    from job_agent.storage.jobs_db import list_runs
    from job_agent.web import runner as run

    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(settings, "outputs_dir", out)
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(json.dumps({"contact": {"full_name": "Asha Verma", "email": "ASHA@Example.org"},
                                        "profile_hash": "h1"}))
    monkeypatch.setattr(settings, "profile_path", profile_file)
    monkeypatch.setattr(run, "build_snapshot", lambda: {})
    monkeypatch.setattr(workflow, "publish_outputs", lambda **kw: {"files": {}, "warnings": []})
    monkeypatch.setitem(run._PHASE_IMPLS, "source", lambda o, c: {"latest_matches": 1})

    run.PipelineRunner().run_sync(["source"], {})

    record = list_runs()[0]
    assert record["candidate_key"] == profile_identity(profile_file) == "asha@example.org"
    assert record["candidate_name"] == "Asha Verma", "the name is for display only"


# ------------------------------------------------------------------ H4 ----------

def test_the_tailoring_panel_does_not_claim_the_text_is_rewritten():
    from job_agent.web.state import PHASE_META

    detail = PHASE_META["tailor"]["detail"].lower()
    assert "rewrites bullets" not in detail
    assert "never" in detail and ("reorder" in detail or "order" in detail), detail


# ------------------------------------------------------------------ icon fonts ---

class _FakePage:
    """The slice of a pdfplumber page that line extraction uses."""

    width = 600

    def __init__(self, chars):
        self._chars = chars

    def filter(self, predicate):
        return _FakePage([c for c in self._chars if predicate(c)])

    def extract_words(self, **kwargs):
        words, cursor = [], 0
        for text in "".join(c["text"] for c in self._chars).split(" "):
            if text:
                words.append({"text": text, "x0": cursor, "x1": cursor + len(text) * 5, "top": 10, "bottom": 20})
                cursor += len(text) * 5 + 4
        return words

    def extract_text(self):
        return "".join(c["text"] for c in self._chars)


def _chars(text, font):
    return [{"text": ch, "fontname": f"ABCDEF+{font}", "object_type": "char"} for ch in text]


def test_icon_font_glyph_names_do_not_leak_into_the_resume_text():
    # A CV that draws an envelope icon with FontAwesome has its glyph read as the word "Envelope",
    # which then glued itself onto the email address and was sealed into the profile.
    from job_agent.intake.layout import extract_page_lines

    page = _FakePage(_chars("Envelope", "FontAwesome5Free-Solid") + _chars(" asha@example.org", "NotoSans-Regular"))
    text = " ".join(extract_page_lines(page))
    assert "Envelope" not in text and "asha@example.org" in text, text


@pytest.mark.parametrize("font,is_icon", [
    ("FontAwesome5Free-Solid", True), ("FontAwesome5Brands-Regular", True), ("MaterialIcons-Regular", True),
    ("Glyphicons Halflings", True), ("Ionicons", True), ("Octicons", True),
    ("NotoSans-Regular", False), ("Arial-BoldMT", False), ("CMSY10", False), ("TimesNewRomanPSMT", False),
    ("Calibri", False), ("SymbolMT", False),
])
def test_only_icon_fonts_are_recognised_as_icons(font, is_icon):
    from job_agent.intake.layout import is_icon_font

    assert is_icon_font(f"ABCDEF+{font}") is is_icon, "maths and symbol fonts carry real text and must be kept"


def test_a_resume_that_is_only_icons_still_returns_its_text(tmp_path):
    from job_agent.intake.layout import extract_page_lines

    page = _FakePage(_chars("Envelope", "FontAwesome5Free-Solid"))
    assert extract_page_lines(page) is not None


def _latex_style_page(path, lines):
    """A PDF the way LaTeX writes it: every word placed by position, with NO space characters.

    That is what the real CV is, and why a text extractor has to decide for itself where one
    word ends and the next begins.
    """
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    for y, size, font, text in lines:
        x = 50.0
        for word in text.split():
            page.insert_text((x, y), word, fontname=font, fontsize=size)
            x += fitz.get_text_length(word, fontname=font, fontsize=size) + size * 0.26   # a 0.26em gap
    doc.save(str(path))
    return path


def test_words_are_not_glued_together_when_a_pdf_has_no_space_characters(tmp_path):
    # pdfplumber's default merges letters closer than 3pt, and a 0.26em space in 10pt text is 2.6pt, so
    # on LaTeX-style PDFs most of the body arrived as "AIresearcherwithexperience". Two of the four
    # resumes tried lost two thirds of their words that way, which is what the intake model was shown.
    import pdfplumber

    from job_agent.intake.layout import extract_page_lines

    path = _latex_style_page(tmp_path / "latex.pdf", [
        (100, 10, "helv", "AI researcher with experience in deep learning and computer vision"),
        (130, 18, "hebo", "ALEX RIVERA"),
    ])
    with pdfplumber.open(path) as pdf:
        text = " | ".join(extract_page_lines(pdf.pages[0]))
    assert "AI researcher with experience in deep learning and computer vision" in text, text
    assert "ALEX RIVERA" in text, "a large heading must not be split into letters"


def test_the_word_gap_is_relative_to_the_text_size(monkeypatch):
    from job_agent.intake import layout

    seen = {}

    class Page(_FakePage):
        def filter(self, predicate):
            return Page([c for c in self._chars if predicate(c)])

        def extract_words(self, **kwargs):
            seen.update(kwargs)
            return super().extract_words()

    layout.extract_page_lines(Page(_chars("hello world", "NotoSans-Regular")))
    assert seen.get("x_tolerance_ratio") == layout.WORD_GAP_RATIO and "x_tolerance" not in seen


def test_linkedin_and_github_addresses_come_from_the_pdfs_hyperlinks(tmp_path):
    import fitz

    from job_agent.intake.parser import pdf_hyperlinks

    path = tmp_path / "cv.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((50, 100), "LinkedIn", fontsize=10)
    page.insert_text((120, 100), "GitHub", fontsize=10)
    page.insert_text((190, 100), "Email", fontsize=10)
    page.insert_text((250, 100), "Portfolio", fontsize=10)
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(50, 90, 100, 104), "uri": "https://www.linkedin.com/in/asha-verma-1234/"})
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(120, 90, 160, 104), "uri": "https://github.com/asha-dev"})
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(190, 90, 230, 104), "uri": "mailto:asha@example.org"})
    page.insert_link({"kind": fitz.LINK_URI, "from": fitz.Rect(250, 90, 300, 104), "uri": "https://github.com/asha-dev/some-repo"})
    doc.save(str(path))

    assert pdf_hyperlinks(path) == {"linkedin": "https://www.linkedin.com/in/asha-verma-1234/",
                                    "github": "https://github.com/asha-dev"}, "a repository link is not a profile"


def test_a_pdf_without_links_or_an_unreadable_one_yields_none(tmp_path):
    from job_agent.intake.parser import pdf_hyperlinks

    bad = tmp_path / "broken.pdf"
    bad.write_bytes(b"not a pdf")
    assert pdf_hyperlinks(bad) == {}
    assert pdf_hyperlinks(tmp_path / "missing.pdf") == {}


# ------------------------------------------------------------------ C1 ----------

def _row(job_id, fit, **extra):
    row = {"Job ID": job_id, "Title": "AI Engineer", "Status": "tailored", "Search Batch": "Current search",
           "Posting Freshness": "Within search window", "Fit Score": fit, "Remote Eligibility": "Open to apply",
           "Tailored Resume": f"resume_{job_id}.pdf"}
    row.update(extra)
    return row


def _readiness(monkeypatch, tmp_path, rows):
    """Run real rows through the exporter's readiness logic with a valid resume for each."""
    import hashlib

    from job_agent.tracking.export import JobsCsvExporter

    out = tmp_path / "outputs"
    folder = out / "tailored_resumes"
    folder.mkdir(parents=True)
    manifest = []
    for job_id in rows:
        pdf = folder / f"resume_{job_id}.pdf"
        pdf.write_bytes(b"%PDF-1.4 resume " + job_id.encode())
        manifest.append({"job_id": job_id, "profile_hash": "p1", "validation_passed": True,
                         "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest()})
    (folder / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(settings, "min_match_score", 7.0)
    exporter = JobsCsvExporter(csv_path=out / "jobs_master.csv", outputs_dir=out)
    exporter._refresh_readiness(rows, "p1")
    return {job_id: (row["Application Readiness"], row["Next Step"]) for job_id, row in rows.items()}


def test_having_a_resume_is_not_the_same_as_being_ready_to_apply(monkeypatch, tmp_path):
    rows = {"strong": _row("strong", "8.3"), "weak": _row("weak", "5.5"), "unscored": _row("unscored", "")}

    states = _readiness(monkeypatch, tmp_path, rows)

    assert states["strong"][0] == "Ready for your review"
    assert states["weak"][0] == "Resume ready, below threshold" and "5.5/10" in states["weak"][1]
    assert states["unscored"][0] == "Resume ready, not scored" and "not been scored" in states["unscored"][1]


def test_exactly_the_threshold_counts_as_ready(monkeypatch, tmp_path):
    states = _readiness(monkeypatch, tmp_path, {"edge": _row("edge", "7.0"), "just_under": _row("just_under", "6.9")})
    assert states["edge"][0] == "Ready for your review"
    assert states["just_under"][0] == "Resume ready, below threshold"


def test_only_scored_and_qualified_jobs_reach_the_ready_list(monkeypatch, tmp_path):
    # The shape of the real run: 188 jobs with a resume, 2 that qualified.
    rows = {f"j{i}": _row(f"j{i}", "8.0" if i < 2 else ("5.0" if i < 52 else "")) for i in range(188)}
    states = _readiness(monkeypatch, tmp_path, rows)
    ready = [job for job, (state, _) in states.items() if state == "Ready for your review"]
    assert len(ready) == 2, f"{len(ready)} of 188 reported ready; only the 2 that qualified should be"


def test_the_funnel_does_not_count_a_resume_as_a_qualification(monkeypatch, tmp_path):
    from job_agent.web import analytics as analytics_module

    rows = [_row(f"j{i}", "8.0" if i < 2 else ("5.0" if i < 12 else ""), **{"Source": "indeed"}) for i in range(30)]

    class Exporter:
        def load(self):
            return {row["Job ID"]: row for row in rows}

    monkeypatch.setattr(analytics_module, "JobsCsvExporter", Exporter)
    monkeypatch.setattr(settings, "outputs_dir", tmp_path)
    monkeypatch.setattr(settings, "min_match_score", 7.0)

    stages = {s["stage"]: s["count"] for s in analytics_module.analytics()["funnel"]}
    assert stages["sourced"] == 30
    assert stages["qualified"] == 2, "it reported every tailored job as qualified"
    assert stages["tailored"] == 30


def test_a_switch_also_retires_the_cumulative_job_sheets_and_ledgers(monkeypatch, tmp_path):
    # jobs_master.csv kept 320 rows scored for the previous candidate, 57 of them >= 7, and the
    # funnel counted them as the new candidate's qualified jobs.
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(settings, "outputs_dir", out)
    monkeypatch.setattr(settings, "profile_path", tmp_path / "profiles" / "profile.json")
    names = ("jobs_master.csv", "jobs_latest.csv", "applications_ready.csv", "application_pack.zip",
             "manual_applications.json", "inbox_events.json")
    for name in names:
        (out / name).write_text("previous candidate")

    result = switch.retire_previous_candidate()

    assert result["complete"], result["problems"]
    for name in names:
        assert not (out / name).exists(), f"{name} still holds the previous candidate's data"
        assert (next((out / "history").glob("*_profile_change")) / name).read_text() == "previous candidate"


# ------------------------------------------------------------------ Phase 3 / H5 --

def test_jobs_list_does_not_rebuild_the_export_on_every_poll(monkeypatch, tmp_path):
    """The dashboard polls /api/jobs; each poll used to re-export under the pipeline lock."""
    import job_agent.web.server as web_server
    from job_agent.tracking.export import JobsCsvExporter

    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(settings, "outputs_dir", out)
    profile_file = tmp_path / "profiles" / "profile.json"
    profile_file.parent.mkdir()
    monkeypatch.setattr(settings, "profile_path", profile_file)
    monkeypatch.setitem(web_server._export_state, "signature", None)

    exports = []
    monkeypatch.setattr(JobsCsvExporter, "export", lambda self: exports.append(1) or out / "jobs_master.csv")
    server = _serve()
    try:
        for _ in range(3):
            _get_json(server, "/api/jobs")
        assert len(exports) == 1, "an unchanged outputs folder must be exported once, not on every poll"

        (out / "evaluated_jobs.json").write_text("[]", encoding="utf-8")   # a stage produced new data
        _get_json(server, "/api/jobs")
        assert len(exports) == 2, "a changed input must trigger a fresh export"
    finally:
        server.shutdown()


def test_a_run_that_just_started_is_never_closed_as_an_orphan(monkeypatch, tmp_path):
    """Phase 4: the 'is anything running?' check and the UPDATE are not atomic."""
    from datetime import datetime, timedelta, timezone

    monkeypatch.setattr(settings, "outputs_dir", tmp_path / "outputs")
    (tmp_path / "outputs").mkdir()
    database = JobsDatabase()
    now = datetime.now(timezone.utc)
    database.save_run({"run_id": "fresh", "started_at": now.isoformat(), "status": "running",
                       "candidate_name": "A", "candidate_key": "a@example.org", "results": {}})
    database.save_run({"run_id": "dead", "started_at": (now - timedelta(minutes=10)).isoformat(),
                       "status": "running", "candidate_name": "A", "candidate_key": "a@example.org", "results": {}})
    assert database.close_orphaned_runs() == 1          # only the old one
    status = {row["run_id"]: row["status"] for row in database.list_runs()}
    assert status == {"fresh": "running", "dead": "interrupted"}
    # The runner holds the lock, so it can close even a recent leftover.
    assert database.close_orphaned_runs(older_than_seconds=0) == 1


def test_a_deleted_database_file_is_prepared_again(tmp_path):
    """Schema creation is skipped for a file this process already prepared, never for a new one."""
    path = tmp_path / "jobs.db"
    JobsDatabase(db_path=path)
    JobsDatabase(db_path=path)                      # second call is the skipped one
    path.unlink()
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"jobs.db{suffix}").unlink(missing_ok=True)
    database = JobsDatabase(db_path=path)           # a brand-new file must get its tables
    with database._connect() as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"jobs", "runs", "run_jobs"} <= tables
    # The view is created after the tables; the skip path must not lose it.
    with database._connect() as conn:
        views = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")}
    assert "job_overview" in views


@pytest.mark.parametrize("raw", [0, 0.0, "0", "0/10", -3, None])
def test_a_disqualifying_zero_fit_score_is_the_worst_valid_score_not_an_error(raw, monkeypatch):
    """Found by the first full-size run: a judge answered fit_score 0 and, in strict mode, the
    whole evaluation phase aborted with a validation error on the 1.0 floor."""
    from job_agent.config.schema import EvaluationScore, RerankerVerdict

    verdict = RerankerVerdict(fit_score=raw, technical_score=raw, seniority_score=raw, reasoning="Not a fit.")
    assert verdict.fit_score == 1.0
    assert verdict.technical_score == 0.0 and verdict.seniority_score == 0.0   # sub-scores may be zero

    score = EvaluationScore(embedding_similarity=0.3, fit_score=raw, technical_score=0.0, seniority_score=0.0,
                            threshold_used=7.0, reasoning="Not a fit.")
    assert score.fit_score == 1.0 and score.passed_threshold is False
