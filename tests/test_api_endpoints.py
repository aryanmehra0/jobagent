"""HTTP-level coverage for the dashboard endpoints that had no test of their own."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from functools import partial

import pytest

from job_agent.config.schema import JobPosting
from job_agent.config.settings import settings
from job_agent.tracking.export import JobsCsvExporter, job_row
from job_agent.web.server import FlowConsoleHandler, FlowConsoleServer

TOKEN = "endpoint-token"


@pytest.fixture
def api(monkeypatch, tmp_path):
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(settings, "outputs_dir", out)
    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(settings, "profile_path", profiles / "profile.json")
    server = FlowConsoleServer(("127.0.0.1", 0), partial(FlowConsoleHandler), TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, payload=None, *, method=None):
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"))
        request.add_header("X-Session-Token", TOKEN)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    call.outputs = out
    call.base = base
    try:
        yield call
    finally:
        server.shutdown()
        server.server_close()


def _seed_one_job(out):
    job = JobPosting(id="job-1", title="AI Engineer", company="Acme", location="Remote", is_remote=True,
                     job_url="https://acme.example/jobs/1", description="Build things. " * 20, source="test")
    row = job_row(job)
    row["Status"] = "found"
    JobsCsvExporter()._write([row])
    return job


def test_analytics_on_an_empty_workspace_is_a_valid_empty_answer(api):
    status, body = api("/api/analytics")
    assert status == 200
    assert {"funnel", "by_score", "by_source"} <= set(body)


def test_performance_report_is_served(api):
    status, body = api("/api/performance")
    assert status == 200 and isinstance(body, dict)


def test_marking_a_job_applied_and_undoing_it_round_trips(api):
    _seed_one_job(api.outputs)
    status, _ = api("/api/jobs/applied", {"job_id": "job-1"})
    assert status == 200
    assert "job-1" in json.loads((api.outputs / "manual_applications.json").read_text(encoding="utf-8"))
    # Applying twice is refused rather than silently repeated.
    assert api("/api/jobs/applied", {"job_id": "job-1"})[0] == 400
    status, _ = api("/api/jobs/applied", {"job_id": "job-1", "undo": True})
    assert status == 200
    assert "job-1" not in json.loads((api.outputs / "manual_applications.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("payload", [{}, {"job_id": 7}, {"job_id": "x", "undo": "yes"}, {"job_id": "never-seen"}])
def test_marking_applied_rejects_bad_input(api, payload):
    status, body = api("/api/jobs/applied", payload)
    assert status == 400 and body["error"]


def test_start_fresh_archives_results_but_keeps_the_seen_jobs_store(api):
    (api.outputs / "evaluated_jobs.json").write_text("[]", encoding="utf-8")
    (api.outputs / "delta_store.db").write_bytes(b"keep me")
    status, body = api("/api/outputs/archive", {})
    assert status == 200 and body["ok"] is True
    assert not (api.outputs / "evaluated_jobs.json").exists()
    assert (api.outputs / "delta_store.db").read_bytes() == b"keep me"
    assert list((api.outputs / "history").glob("*/evaluated_jobs.json")), "the results are archived, not deleted"


def test_application_pack_is_built_from_the_shortlist(api):
    _seed_one_job(api.outputs)
    status, body = api("/api/export/bundle", {})
    assert status == 200
    from pathlib import Path
    assert Path(body["path"]).name == "application_pack.zip" and Path(body["path"]).is_file()


def test_the_jobs_list_can_be_narrowed_to_one_runs_shortlist(api):
    _seed_one_job(api.outputs)
    status, body = api("/api/jobs")
    assert status == 200 and [row["Job ID"] for row in body["jobs"]] == ["job-1"]
    status, body = api("/api/jobs?run=no-such-run")
    assert status == 200 and body["jobs"] == []


def test_mutating_endpoints_need_the_session_token(api):
    request = urllib.request.Request(
        api.base + "/api/outputs/archive", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=10)
    assert error.value.code == 401


def test_skipped_jobs_endpoint_lists_reasons_and_filters(api):
    (api.outputs / "skipped_jobs.json").write_text(json.dumps([
        {"reason": "off_target", "title": "Sales Engineer", "company": "A", "location": "X", "source": "indeed", "url": "u1"},
        {"reason": "off_target", "title": "Nurse", "company": "B", "location": "Y", "source": "indeed", "url": "u2"},
        {"reason": "too_old", "title": "AI Engineer", "company": "C", "location": "Z", "source": "linkedin", "url": "u3"},
    ]), encoding="utf-8")
    status, body = api("/api/skipped")
    assert status == 200 and body["total"] == 3 and body["reasons"] == {"off_target": 2, "too_old": 1}
    status, body = api("/api/skipped?reason=too_old")
    assert [j["title"] for j in body["jobs"]] == ["AI Engineer"]
    assert api("/api/skipped?limit=zzz")[0] == 200


def test_skipped_jobs_endpoint_is_empty_not_an_error_before_any_sweep(api):
    status, body = api("/api/skipped")
    assert status == 200 and body == {"total": 0, "reasons": {}, "jobs": []}


def test_skipping_a_job_is_recorded_on_the_server_and_can_be_undone(api):
    from job_agent.sourcing.delta_store import DeltaStore

    job = _seed_one_job(api.outputs)
    DeltaStore().mark_many_seen([job])
    status, body = api("/api/jobs/skip", {"job_id": "job-1", "reason": "Needs 8 years"})
    assert status == 200 and body["skipped"] == ["job-1"]
    saved = json.loads((api.outputs / "user_skips.json").read_text(encoding="utf-8"))
    assert saved["job-1"]["reason"] == "Needs 8 years" and saved["job-1"]["company"] == "Acme"
    assert DeltaStore().statuses(["job-1"])["job-1"] == "skipped"
    assert api("/api/jobs")[1]["user_skipped"] == ["job-1"]
    # Not an application outcome: the tracker's applied marker is untouched.
    assert not (api.outputs / "manual_applications.json").exists()
    assert api("/api/jobs/skip", {"job_id": "job-1"})[0] == 400          # already skipped

    status, body = api("/api/jobs/skip", {"job_id": "job-1", "undo": True})
    assert status == 200 and body["skipped"] == []
    assert DeltaStore().statuses(["job-1"])["job-1"] == "scraped"


@pytest.mark.parametrize("payload", [{}, {"job_id": 3}, {"job_id": "x", "reason": 5}, {"job_id": "never-seen"},
                                     {"job_id": "job-1", "undo": True}])
def test_skip_rejects_bad_input(api, payload):
    _seed_one_job(api.outputs)
    status, body = api("/api/jobs/skip", payload)
    assert status == 400 and body["error"]


# ---- Phase 6: the profile switch, end to end over HTTP ------------------------------------------

def _resume_pdf(path, name, email):
    """The bundled fictional resume with a different person's name and email."""
    import fitz
    from pathlib import Path

    sample = Path(__file__).resolve().parents[1] / "data" / "raw_resumes" / "sample_resume.pdf"
    with fitz.open(sample) as source:
        text = source[0].get_text().replace("Alex Rivera", name).replace("alex.rivera@example.com", email)
        text = text.replace("alexrivera", email.split("@")[0])
    document = fitz.open()
    page = document.new_page()
    page.insert_textbox(fitz.Rect(36, 36, 560, 800), text, fontsize=8)
    document.save(path)
    return path.read_bytes()


def test_loading_a_different_candidates_resume_leaves_nothing_of_the_previous_one(api, monkeypatch, tmp_path):
    import time

    from job_agent.config.settings import settings
    from job_agent.sourcing.delta_store import DeltaStore

    monkeypatch.setattr(settings, "raw_resumes_dir", tmp_path / "raw")
    monkeypatch.setattr(settings, "tracker_path", api.outputs / "applications_tracker.xlsx", raising=False)
    first = _resume_pdf(tmp_path / "a.pdf", "Alex Rivera", "alex.rivera@example.com")
    second = _resume_pdf(tmp_path / "b.pdf", "Priya Nair", "priya.nair@example.org")

    def run_intake(pdf_bytes, filename):
        request = urllib.request.Request(api.base + "/api/resume", data=pdf_bytes, method="POST")
        request.add_header("X-Session-Token", TOKEN)
        request.add_header("X-Filename", filename)
        urllib.request.urlopen(request, timeout=20).read()
        assert api("/api/run", {"phases": ["intake"], "options": {"resume": str(settings.raw_resumes_dir / filename)}})[0] == 202
        for _ in range(300):
            if not api("/api/state")[1]["running"]:
                return
            time.sleep(0.1)
        raise AssertionError("intake did not finish")

    run_intake(first, "a.pdf")
    profile = json.loads(settings.profile_path.read_text(encoding="utf-8"))
    assert profile["contact"]["email"] == "alex.rivera@example.com"

    # The first candidate's results, as a finished run would have left them.
    job = _seed_one_job(api.outputs)
    DeltaStore().mark_many_seen([job])
    (api.outputs / "evaluated_jobs.json").write_text(json.dumps([{"job": job.model_dump()}]), encoding="utf-8")
    (api.outputs / "user_skips.json").write_text(json.dumps({"job-1": {"at": "x"}}), encoding="utf-8")
    assert api("/api/runs")[1]["current_profile"] == "alex.rivera@example.com"

    run_intake(second, "b.pdf")

    profile = json.loads(settings.profile_path.read_text(encoding="utf-8"))
    assert profile["contact"]["email"] == "priya.nair@example.org"
    assert "Alex" not in json.dumps(profile) and "alex.rivera" not in json.dumps(profile)
    assert api("/api/runs")[1]["current_profile"] == "priya.nair@example.org"
    # The previous candidate's results and decisions are gone from the live workspace...
    assert not (api.outputs / "evaluated_jobs.json").exists()
    assert not (api.outputs / "user_skips.json").exists()
    assert api("/api/jobs")[1]["user_skipped"] == []
    # ...but kept, in one archive, rather than deleted.
    assert list((api.outputs / "history").glob("*/evaluated_jobs.json")), "the earlier results are archived"
    assert list((api.outputs / "history").glob("*_profile_change/user_skips.json")), "so are the earlier decisions"
