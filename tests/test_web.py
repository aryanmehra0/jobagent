"""Tests for the flow console: state snapshot, runner guards, and HTTP security.

The security tests matter more than usual here. The console can start a browser
and submit real job applications, so the guards that stop a hostile page from
reaching it are load-bearing, not decorative.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer
from typing import Optional, Tuple

import pytest

from job_agent.web import runner as runner_module
from job_agent.web.server import FlowConsoleHandler, FlowConsoleServer
from job_agent.web.state import PHASE_META, PHASE_ORDER, build_snapshot


# ==============================================================================
# STATE SNAPSHOT
# ==============================================================================

def test_snapshot_describes_every_phase():
    """The dashboard draws its graph from this, so no phase may be missing."""
    snapshot = build_snapshot()

    assert snapshot["order"] == PHASE_ORDER
    for name in PHASE_ORDER:
        phase = snapshot["phases"][name]
        assert phase["status"] in {"empty", "ready", "error"}
        assert phase["title"] and phase["summary"]
        assert "command" in phase, "each node shows its CLI equivalent"
        assert isinstance(phase["metrics"], dict)


def test_snapshot_reports_provider_and_paths():
    snapshot = build_snapshot()
    assert snapshot["provider"] in {"openai", "anthropic", "groq", "openai_compatible", "none"}
    assert set(snapshot["llm"]["supported_providers"]) >= {"openai", "anthropic", "groq", "openai_compatible", "none"}
    assert snapshot["paths"]["outputs"]
    assert "tier1" in snapshot["thresholds"] and "fit" in snapshot["thresholds"]


def test_snapshot_is_json_serialisable():
    """It is sent over the wire verbatim; a stray Path would break the console."""
    assert json.loads(json.dumps(build_snapshot(), default=str))


def test_phase_metadata_covers_every_phase():
    assert set(PHASE_META) == set(PHASE_ORDER)


# ==============================================================================
# RUNNER
# ==============================================================================

def test_runner_rejects_unknown_phases():
    runner = runner_module.PipelineRunner()
    assert "Unknown phase" in runner.start(["not_a_phase"], {})
    assert runner.start([], {}) == "No phases selected."


def test_runner_refuses_a_second_concurrent_run():
    """Phase output is captured by redirecting process-wide stdout, and phases
    share artifact files; two at once would interleave both."""
    runner = runner_module.PipelineRunner()
    release = threading.Event()

    def slow_phase(options, cancel):
        release.wait(timeout=5)
        return {"ok": True}

    original = runner_module._PHASE_IMPLS["track"]
    runner_module._PHASE_IMPLS["track"] = slow_phase
    try:
        assert runner.start(["track"], {}) is None
        # Give the worker thread a moment to take the lock.
        for _ in range(50):
            if runner.is_running:
                break
            time.sleep(0.02)
        assert runner.start(["track"], {}) == "A run is already in progress."
    finally:
        release.set()
        runner_module._PHASE_IMPLS["track"] = original
        for _ in range(100):
            if not runner.is_running:
                break
            time.sleep(0.02)


def test_runner_publishes_lifecycle_events():
    """Subscribers must see start, the phase boundary, and the terminal event."""
    runner = runner_module.PipelineRunner()
    channel = runner.subscribe()

    original = runner_module._PHASE_IMPLS["track"]
    runner_module._PHASE_IMPLS["track"] = lambda options, cancel: {"logged": 3}
    try:
        assert runner.start(["track"], {}) is None
        events = _drain(channel, until="run_end", timeout=10)
    finally:
        runner_module._PHASE_IMPLS["track"] = original

    kinds = [event["type"] for event in events]
    assert kinds[0] == "run_start"
    assert "phase_start" in kinds and "state" in kinds
    assert kinds[-1] == "run_end"

    ended = next(e for e in events if e["type"] == "phase_end")
    assert ended["phase"] == "track"
    assert ended["status"] == "ok"
    assert ended["summary"]["logged"] == 3


def test_runner_reports_a_failing_phase_without_raising():
    """One bad phase must surface as an error event, not kill the worker."""
    runner = runner_module.PipelineRunner()
    channel = runner.subscribe()

    def boom(options, cancel):
        raise RuntimeError("phase exploded")

    original = runner_module._PHASE_IMPLS["track"]
    runner_module._PHASE_IMPLS["track"] = boom
    try:
        runner.start(["track"], {})
        events = _drain(channel, until="run_end", timeout=10)
    finally:
        runner_module._PHASE_IMPLS["track"] = original

    ended = next(e for e in events if e["type"] == "phase_end")
    assert ended["status"] == "error"
    assert "phase exploded" in ended["error"]
    assert events[-1]["status"] == "error"


def test_runner_halts_the_chain_when_a_phase_produces_nothing():
    """Running `evaluate` after a sweep that found no jobs is meaningless."""
    runner = runner_module.PipelineRunner()
    channel = runner.subscribe()

    originals = dict(runner_module._PHASE_IMPLS)
    runner_module._PHASE_IMPLS["source"] = lambda o, c: {"found": 0, "halt_reason": "no new jobs"}
    runner_module._PHASE_IMPLS["evaluate"] = lambda o, c: pytest.fail("must not run after a halt")
    try:
        runner.start(["source", "evaluate"], {})
        events = _drain(channel, until="run_end", timeout=10)
    finally:
        runner_module._PHASE_IMPLS.update(originals)

    assert events[-1]["status"] == "halted"
    assert not any(e["type"] == "phase_start" and e["phase"] == "evaluate" for e in events)


def test_line_tee_splits_output_into_whole_lines():
    """The log pane renders one event per line, so partial writes must be buffered."""
    captured = []
    tee = runner_module._LineTee(captured.append)
    tee.write("first line\nsecond ")
    assert captured == ["first line"]
    tee.write("half\n")
    assert captured == ["first line", "second half"]
    tee.write("no trailing newline")
    tee.flush()
    assert captured[-1] == "no trailing newline"


def _drain(channel, until: str, timeout: float = 5.0):
    """Collect events until the named type arrives, or the timeout expires."""
    events = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            event = channel.get(timeout=0.2)
        except Exception:
            continue
        events.append(event)
        if event["type"] == until:
            return events
    raise AssertionError(f"Never saw a {until!r} event; got {[e['type'] for e in events]}")


# ==============================================================================
# HTTP SECURITY
# ==============================================================================

@pytest.fixture(scope="module")
def console() -> Tuple[str, str]:
    """Run the console on an ephemeral loopback port for the duration of the module."""
    server = FlowConsoleServer(("127.0.0.1", 0), partial(FlowConsoleHandler), "test-token-abc123")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", server.token
    finally:
        server.shutdown()
        server.server_close()


def _request(
    url: str,
    method: str = "GET",
    token: Optional[str] = None,
    body: Optional[bytes] = None,
    headers: Optional[dict] = None,
) -> Tuple[int, bytes]:
    """Issue a request and return (status, body) without raising on 4xx/5xx."""
    request = urllib.request.Request(url, method=method, data=body)
    if token:
        request.add_header("X-Session-Token", token)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def test_dashboard_is_served_with_the_token_injected(console):
    base, token = console
    status, body = _request(f"{base}/")
    assert status == 200
    page = body.decode("utf-8")
    assert token in page
    assert "__SESSION_TOKEN__" not in page, "the placeholder must be substituted"


def test_state_endpoint_returns_a_snapshot(console):
    base, _ = console
    status, body = _request(f"{base}/api/state")
    assert status == 200
    payload = json.loads(body)
    assert payload["state"]["order"] == PHASE_ORDER
    assert payload["running"] is False


def test_mutating_endpoints_require_the_session_token(console):
    """Without this, any page in the browser could start a run on localhost."""
    base, _ = console
    for path in ("/api/run", "/api/config", "/api/llm", "/api/resume", "/api/cancel"):
        status, _ = _request(f"{base}{path}", method="POST", body=b"{}")
        assert status == 401, f"{path} accepted an unauthenticated POST"


def test_a_wrong_token_is_rejected(console):
    base, _ = console
    status, _ = _request(f"{base}/api/run", "POST", token="not-the-token", body=b"{}")
    assert status == 401


def test_cross_origin_requests_are_rejected(console):
    """A page on the open web must not be able to drive the console."""
    base, token = console
    status, _ = _request(
        f"{base}/api/run", "POST", token=token, body=b"{}",
        headers={"Origin": "https://evil.example.com"},
    )
    assert status == 403


def test_rebound_host_header_is_rejected(console):
    """Closes the DNS-rebinding path around the loopback bind."""
    base, _ = console
    status, _ = _request(f"{base}/api/state", headers={"Host": "evil.example.com"})
    assert status == 403


def test_live_applying_requires_explicit_confirmation(console):
    """No single request may cause real applications to be submitted."""
    base, token = console
    payload = json.dumps({"phases": ["apply"], "options": {"dry_run": False}}).encode()
    status, body = _request(f"{base}/api/run", "POST", token=token, body=payload,
                            headers={"Content-Type": "application/json"})
    assert status == 400
    assert "confirmation" in json.loads(body)["error"].lower()


def test_artifact_route_refuses_paths_outside_the_data_directory(console):
    """The console must not double as a file browser for the whole machine."""
    base, _ = console
    status, body = _request(f"{base}/api/file?path=C:/Windows/System32/drivers/etc/hosts")
    assert status == 403
    assert "outside" in json.loads(body)["error"]


def test_resume_upload_rejects_non_pdf_content(console):
    base, token = console
    status, body = _request(
        f"{base}/api/resume", "POST", token=token, body=b"this is not a pdf",
        headers={"X-Filename": "resume.pdf"},
    )
    assert status == 400
    assert "not a PDF" in json.loads(body)["error"]


def test_config_endpoint_surfaces_validation_errors(console):
    """A bad board name must come back naming the field, not as a 500."""
    base, token = console
    payload = json.dumps({
        "target_domains": ["Engineer"], "locations": ["Remote"], "job_boards": ["monster"],
    }).encode()
    status, body = _request(f"{base}/api/config", "POST", token=token, body=payload,
                            headers={"Content-Type": "application/json"})
    assert status == 400
    assert "job_boards" in json.loads(body)["error"]


def test_llm_settings_are_saved_without_returning_the_secret(tmp_path, monkeypatch):
    from job_agent.config.settings import settings as live_settings
    import job_agent.config.settings as settings_module
    from job_agent.web.server import _save_llm_settings
    from job_agent.web.state import build_snapshot

    env_file = tmp_path / ".env"
    env_file.write_text("DEFAULT_LLM_PROVIDER=none\n", encoding="utf-8")
    monkeypatch.setattr(settings_module, "dotenv_path", env_file)
    monkeypatch.setattr(live_settings, "default_llm_provider", "none")
    monkeypatch.setattr(live_settings, "groq_api_keys", live_settings.groq_api_keys.__class__(""))

    provider = _save_llm_settings({
        "provider": "groq",
        "groq_api_keys": "gsk_test_key",
        "groq_model": "openai/gpt-oss-120b",
        "groq_fallback_model": "openai/gpt-oss-20b",
    })

    assert provider == "groq"
    assert "GROQ_API_KEYS=gsk_test_key" in env_file.read_text(encoding="utf-8")
    snapshot = build_snapshot()
    assert snapshot["llm"]["has_keys"]["groq"] is True
    assert "gsk_test_key" not in json.dumps(snapshot)


def test_unknown_routes_return_404(console):
    base, _ = console
    status, _ = _request(f"{base}/api/nope")
    assert status == 404


# ==============================================================================
# DEMO-DATA DETECTION AND THE FIRST-RUN WIZARD
# ==============================================================================

@pytest.fixture
def isolated_data(tmp_path, monkeypatch):
    """Point the agent's data paths at a temporary directory.

    `settings` is a module-level singleton, so every attribute the state builder
    reads is redirected and restored by monkeypatch when the test ends.
    """
    from job_agent.config.settings import settings as live_settings

    resumes = tmp_path / "raw_resumes"
    outputs = tmp_path / "outputs"
    resumes.mkdir(parents=True)
    outputs.mkdir(parents=True)

    monkeypatch.setattr(live_settings, "raw_resumes_dir", resumes)
    monkeypatch.setattr(live_settings, "outputs_dir", outputs)
    monkeypatch.setattr(live_settings, "profile_path", tmp_path / "profile.json")
    monkeypatch.setattr(live_settings, "tracker_path", outputs / "tracker.xlsx")
    monkeypatch.setattr(live_settings, "searches_path", tmp_path / "searches.yaml")
    monkeypatch.setattr(live_settings, "data_dir", tmp_path)
    return tmp_path


def _write_profile(path, *, source_document: str, name: str = "Test Person"):
    """Write a minimal sealed profile that records which resume produced it."""
    from job_agent.config.schema import (
        CandidateProfile, ContactInfo, SkillSet, WorkAuthorization,
    )

    profile = CandidateProfile(
        contact=ContactInfo(full_name=name, email="test@example.com", location="Remote"),
        summary="Engineer with a background in backend systems and infrastructure.",
        work_authorization=WorkAuthorization(current_country="United States"),
        skills=SkillSet(languages=["Python"]),
        years_of_experience=4.0,
        source_document=source_document,
    ).seal_profile()
    path.write_text(profile.model_dump_json(indent=2), encoding="utf-8")


def test_setup_flags_a_fresh_install_as_needing_a_resume(isolated_data):
    """With nothing on disk the wizard must open and the pipeline must stay gated."""
    setup = build_snapshot()["setup"]
    assert setup["needs_resume"] is True
    assert setup["needs_profile"] is True
    assert setup["using_sample"] is False
    assert setup["ready"] is False


def test_tracking_state_includes_outreach_counts(isolated_data):
    from job_agent.config.settings import settings as live_settings
    from job_agent.config.schema import JobPosting
    from job_agent.sourcing.delta_store import DeltaStore

    store = DeltaStore(db_path=live_settings.outputs_dir / "delta_store.db")
    job = JobPosting(
        id="outreach-state-1",
        title="AI Product Manager",
        company="Acme",
        job_url="https://acme.test/jobs/1",
        description="Build AI products.",
        source="greenhouse",
    )
    store.record_outreach("careers@acme.test", job, "Subject", "Body")

    metrics = build_snapshot()["phases"]["track"]["metrics"]
    assert metrics["Email drafts"] == 1
    assert metrics["Unique inboxes"] == 1
    assert metrics["Roles drafted"] == 1


def test_setup_flags_the_bundled_sample_profile_as_demo_data(isolated_data):
    """Running the pipeline on this would apply as the demo candidate."""
    from job_agent.web.state import SAMPLE_RESUME_NAME
    from job_agent.config.settings import settings as live_settings

    (live_settings.raw_resumes_dir / SAMPLE_RESUME_NAME).write_bytes(b"%PDF-1.4 stub")
    _write_profile(live_settings.profile_path, source_document=SAMPLE_RESUME_NAME, name="Alex Rivera")

    snapshot = build_snapshot()
    assert snapshot["phases"]["intake"]["is_sample"] is True
    assert "demo profile" in snapshot["phases"]["intake"]["hint"]
    assert snapshot["setup"]["using_sample"] is True
    assert snapshot["setup"]["needs_resume"] is True, "only the sample is present"
    assert snapshot["setup"]["ready"] is False


def test_a_real_resume_clears_the_demo_warning(isolated_data):
    from job_agent.config.settings import settings as live_settings
    from job_agent.web.state import SAMPLE_RESUME_NAME

    (live_settings.raw_resumes_dir / SAMPLE_RESUME_NAME).write_bytes(b"%PDF-1.4 stub")
    (live_settings.raw_resumes_dir / "my_resume.pdf").write_bytes(b"%PDF-1.4 stub")
    _write_profile(live_settings.profile_path, source_document="my_resume.pdf", name="Priya Raman")
    live_settings.searches_path.write_text(
        "target_domains: [Backend Engineer]\nlocations: [Remote]\njob_boards: [linkedin]\n",
        encoding="utf-8",
    )

    snapshot = build_snapshot()
    assert snapshot["setup"]["using_sample"] is False
    assert snapshot["setup"]["needs_resume"] is False
    assert snapshot["setup"]["ready"] is True
    assert snapshot["phases"]["intake"]["hint"] == "Fact seal verified."


def test_real_resumes_sort_ahead_of_the_bundled_sample(isolated_data):
    """The console preselects resumes[0]; it must never preselect demo data."""
    import os
    import time

    from job_agent.config.settings import settings as live_settings
    from job_agent.web.state import SAMPLE_RESUME_NAME, available_resumes

    mine = live_settings.raw_resumes_dir / "my_resume.pdf"
    sample = live_settings.raw_resumes_dir / SAMPLE_RESUME_NAME
    mine.write_bytes(b"%PDF-1.4 stub")
    sample.write_bytes(b"%PDF-1.4 stub")
    # Make the sample the newest file, so recency alone would put it first.
    os.utime(sample, (time.time() + 60, time.time() + 60))

    resumes = available_resumes()
    assert resumes[0]["name"] == "my_resume.pdf"
    assert resumes[0]["is_sample"] is False
    assert resumes[-1]["is_sample"] is True


def test_delete_resume_requires_a_token(console):
    base, _ = console
    status, _ = _request(f"{base}/api/resume/delete", "POST", body=b'{"name":"x.pdf"}')
    assert status == 401


def test_delete_resume_rejects_paths_outside_the_resumes_directory(console):
    """The endpoint exists to clear the demo resume, not to delete arbitrary files."""
    base, token = console
    for name in ("../../profile.json", "C:/Windows/notepad.exe", "notes.txt"):
        payload = json.dumps({"name": name}).encode()
        status, _ = _request(f"{base}/api/resume/delete", "POST", token=token, body=payload,
                             headers={"Content-Type": "application/json"})
        assert status in (400, 404), f"{name} was not refused"


def test_delete_resume_removes_a_real_file(console, tmp_path, monkeypatch):
    from job_agent.config.settings import settings as live_settings

    resumes = tmp_path / "raw_resumes"
    resumes.mkdir()
    victim = resumes / "throwaway.pdf"
    victim.write_bytes(b"%PDF-1.4 stub")
    monkeypatch.setattr(live_settings, "raw_resumes_dir", resumes)

    base, token = console
    status, body = _request(f"{base}/api/resume/delete", "POST", token=token,
                            body=json.dumps({"name": "throwaway.pdf"}).encode(),
                            headers={"Content-Type": "application/json"})
    assert status == 200
    assert json.loads(body)["removed"] == "throwaway.pdf"
    assert not victim.exists()


# ==============================================================================
# MULTI-FORMAT UPLOAD AND THE READINESS ENDPOINT
# ==============================================================================

def test_snapshot_advertises_the_supported_formats():
    """The uploader's accept filter is driven by this list."""
    formats = build_snapshot()["formats"]
    assert ".pdf" in formats and ".docx" in formats


def test_upload_accepts_a_docx(console, tmp_path, monkeypatch):
    from job_agent.config.settings import settings as live_settings

    resumes = tmp_path / "raw_resumes"
    resumes.mkdir()
    monkeypatch.setattr(live_settings, "raw_resumes_dir", resumes)

    base, token = console
    # A .docx is a zip archive, so its magic bytes are "PK".
    status, _ = _request(
        f"{base}/api/resume", "POST", token=token, body=b"PK\x03\x04 fake docx body",
        headers={"X-Filename": "resume.docx"},
    )
    assert status == 200
    assert (resumes / "resume.docx").exists()


def test_upload_rejects_content_that_contradicts_the_extension(console, tmp_path, monkeypatch):
    """A .doc renamed to .docx would otherwise fail confusingly during extraction."""
    from job_agent.config.settings import settings as live_settings

    resumes = tmp_path / "raw_resumes"
    resumes.mkdir()
    monkeypatch.setattr(live_settings, "raw_resumes_dir", resumes)

    base, token = console
    status, body = _request(
        f"{base}/api/resume", "POST", token=token, body=b"\xd0\xcf\x11\xe0 old doc",
        headers={"X-Filename": "resume.docx"},
    )
    assert status == 400
    assert ".docx" in json.loads(body)["error"]


def test_upload_rejects_an_unsupported_extension(console):
    base, token = console
    status, body = _request(
        f"{base}/api/resume", "POST", token=token, body=b"whatever",
        headers={"X-Filename": "resume.rtf"},
    )
    assert status == 400
    assert "Accepted:" in json.loads(body)["error"]


def test_readiness_endpoint_reports_on_a_resume(console, tmp_path, monkeypatch):
    from job_agent.config.settings import settings as live_settings

    resumes = tmp_path / "raw_resumes"
    resumes.mkdir()
    (resumes / "candidate.txt").write_text(
        "ANITA DESAI\nanita@example.com | +91 98765 43210 | Pune, India\n"
        "WORK EXPERIENCE\nZeta - Senior Engineer (2021 - Present)\n"
        "- Cut latency by 43% across 2.1M requests.\n"
        "SKILLS\nLanguages: Python, Go, SQL\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(live_settings, "raw_resumes_dir", resumes)

    base, token = console
    status, body = _request(
        f"{base}/api/resume/check", "POST", token=token,
        body=json.dumps({"name": "candidate.txt"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 200
    report = json.loads(body)["report"]
    assert report["summary"]["name"] == "ANITA DESAI"
    assert report["summary"]["roles"] == 1
    assert isinstance(report["findings"], list)


def test_readiness_endpoint_requires_a_token(console):
    base, _ = console
    status, _ = _request(f"{base}/api/resume/check", "POST", body=b'{"name":"x.pdf"}')
    assert status == 401


def test_readiness_endpoint_refuses_paths_outside_the_resumes_directory(console):
    base, token = console
    status, _ = _request(
        f"{base}/api/resume/check", "POST", token=token,
        body=json.dumps({"name": "../../profile.json"}).encode(),
        headers={"Content-Type": "application/json"},
    )
    assert status == 404
