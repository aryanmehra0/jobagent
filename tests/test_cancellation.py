"""Stop must take effect inside a phase, at safe points only.

The dashboard's Stop button previously took effect only *between* phases. A
sourcing sweep runs for twenty minutes or more, so Stop appeared to do nothing.
Each long-running loop now checks for cancellation before starting its next unit
of work — never in the middle of one, and never between submitting an
application and recording that it was submitted.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from job_agent.config.settings import settings
from job_agent.runtime import RunCancelled, cancellation, check_cancelled


def test_check_is_a_no_op_outside_a_cancellable_run():
    """The CLI never installs a token, so the checks must cost nothing there."""
    check_cancelled()


def test_check_raises_once_the_token_is_set():
    event = threading.Event()
    with cancellation(event):
        check_cancelled()
        event.set()
        with pytest.raises(RunCancelled):
            check_cancelled()
    check_cancelled()  # the token is removed on exit


def test_the_token_is_scoped_to_its_own_thread():
    """A run in the dashboard's worker thread must not cancel an unrelated CLI call."""
    event = threading.Event()
    event.set()
    errors = []

    def other_thread():
        try:
            check_cancelled()
        except RunCancelled as exc:
            errors.append(exc)

    with cancellation(event):
        worker = threading.Thread(target=other_thread)
        worker.start()
        worker.join()
    assert errors == []


# ==============================================================================
# SOURCING
# ==============================================================================

class _SlowJobSpy:
    def __init__(self, event, cancel_after):
        self.event = event
        self.cancel_after = cancel_after
        self.calls = 0

    def scrape_jobs(self, **kwargs):
        import pandas as pd

        self.calls += 1
        if self.calls == self.cancel_after:
            self.event.set()  # the user presses Stop during this request
        return pd.DataFrame()


def test_sourcing_stops_before_the_next_board_request(tmp_path: Path, monkeypatch):
    from job_agent.config.schema import SearchParameters
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.sourcing.scraper import OmnichannelScraper

    params = SearchParameters(target_domains=["APM", "AI PM", "ML Engineer"],
                              locations=["Remote", "Mumbai", "Delhi"],
                              job_boards=["linkedin", "indeed"], is_remote=False)
    scraper = OmnichannelScraper(search_params=params, delta_store=DeltaStore(db_path=tmp_path / "d.db"))
    event = threading.Event()
    fake = _SlowJobSpy(event, cancel_after=2)

    import sys
    monkeypatch.setitem(sys.modules, "jobspy", fake)

    with cancellation(event), pytest.raises(RunCancelled):
        scraper.scrape_job_boards()

    assert fake.calls == 2, "the request in flight finishes; no further request starts"


def test_a_cancelled_sweep_writes_no_partial_results(tmp_path: Path, monkeypatch):
    """Half a sweep must not be recorded as seen, or those jobs are never found again."""
    from job_agent.config.schema import SearchParameters
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.sourcing.scraper import OmnichannelScraper

    params = SearchParameters(target_domains=["APM"], locations=["Remote", "Mumbai"],
                              job_boards=["linkedin"], is_remote=False)
    store = DeltaStore(db_path=tmp_path / "d.db")
    scraper = OmnichannelScraper(search_params=params, delta_store=store)
    event = threading.Event()

    import sys
    monkeypatch.setitem(sys.modules, "jobspy", _SlowJobSpy(event, cancel_after=1))

    output = tmp_path / "scraped_jobs.json"
    with cancellation(event), pytest.raises(RunCancelled):
        scraper.run_sourcing_pipeline(include_ats_direct=False, output_file=output)

    assert not output.exists()
    assert store.get_seen_count() == 0


# ==============================================================================
# EVALUATION AND TAILORING
# ==============================================================================

def test_evaluation_stops_between_jobs_and_keeps_its_progress(monkeypatch):
    """Stopping must not discard scores already paid for; re-running resumes."""
    from job_agent.evaluation.embedder import SemanticEmbedder
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline
    from job_agent.sourcing.delta_store import DeltaStore
    from tests.test_live_resilience import _CountingReranker, _seed_profile_and_jobs

    outputs = settings.outputs_dir
    profile_path, jobs_path = _seed_profile_and_jobs(outputs, count=5)
    store = DeltaStore(db_path=outputs / "delta.db")
    event = threading.Event()

    class StopAfterTwo(_CountingReranker):
        def evaluate_job(self, profile, job, similarity):
            result = super().evaluate_job(profile, job, similarity)
            if len(self.scored_ids) == 2:
                event.set()
            return result

    first = StopAfterTwo()
    with cancellation(event), pytest.raises(RunCancelled):
        SemanticEvaluationPipeline(embedder=SemanticEmbedder(), reranker=first, delta_store=store).run_evaluation(
            profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=0.0, output_dir=outputs,
        )
    assert len(first.scored_ids) == 2

    second = _CountingReranker()
    evaluated, _ = SemanticEvaluationPipeline(
        embedder=SemanticEmbedder(), reranker=second, delta_store=store,
    ).run_evaluation(profile_path=profile_path, jobs_path=jobs_path, tier1_threshold=0.0, output_dir=outputs)
    assert len(second.scored_ids) == 3
    assert len(evaluated) == 5


def test_tailoring_stops_between_jobs(monkeypatch):
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tailoring.compiler import TypstResumeCompiler
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline
    from tests.test_live_resilience import _FlakyTailorer, _seed_profile_and_jobs, _seed_qualified

    outputs = settings.outputs_dir
    profile_path, _ = _seed_profile_and_jobs(outputs, count=3)
    qualified_path = _seed_qualified(outputs, count=3)
    event = threading.Event()

    class StopAfterOne(_FlakyTailorer):
        def generate_tailored_profile_data(self, profile, job):
            result = super().generate_tailored_profile_data(profile, job)
            event.set()
            return result

    tailorer = StopAfterOne()
    with cancellation(event), pytest.raises(RunCancelled):
        ResumeTailoringPipeline(
            tailorer=tailorer, compiler=TypstResumeCompiler(output_dir=outputs / "tailored_resumes"),
            delta_store=DeltaStore(db_path=outputs / "delta.db"),
        ).run_tailoring(profile_path=profile_path, qualified_jobs_path=qualified_path)
    assert len(tailorer.tailored_ids) == 1


# ==============================================================================
# AUTO-APPLY: never between submit and record
# ==============================================================================

def test_apply_honours_stop_before_a_job_starts(tmp_path: Path):
    from job_agent.automation.pipeline import AutoApplyPipeline

    pipeline = AutoApplyPipeline.__new__(AutoApplyPipeline)
    event = threading.Event()
    event.set()
    with cancellation(event), pytest.raises(RunCancelled):
        pipeline._check_before_job()


def test_apply_never_abandons_a_job_after_submit_was_clicked():
    """Stopping after Submit could record a real application as failed."""
    from job_agent.automation.agent import AutoApplyAgent

    event = threading.Event()
    event.set()
    with cancellation(event):
        # Before submitting, Stop is honoured...
        with pytest.raises(RunCancelled):
            AutoApplyAgent._check_between_steps(submit_attempted=False)
        # ...but once Submit has been clicked, the job runs to its recorded outcome.
        AutoApplyAgent._check_between_steps(submit_attempted=True)


# ==============================================================================
# TRACKING
# ==============================================================================

def test_tracking_saves_rows_already_logged_when_stopped(tmp_path: Path, monkeypatch):
    """Rows were only saved after the loop, so stopping lost every one of them."""
    import json

    import openpyxl

    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tracking.pipeline import FallbackTrackingPipeline
    from job_agent.tracking.tracker import MasterTracker
    from tests.test_live_resilience import _seed_profile_and_jobs, _seed_qualified

    outputs = settings.outputs_dir
    profile_path, _ = _seed_profile_and_jobs(outputs, count=3)
    qualified_path = _seed_qualified(outputs, count=3)
    workbook = tmp_path / "tracker.xlsx"
    event = threading.Event()

    class StopAfterFirstEmail:
        calls = 0

        def generate_email(self, profile, job, fit_score=0.0):
            StopAfterFirstEmail.calls += 1
            if StopAfterFirstEmail.calls == 1:
                event.set()
            return f"Subject: {job.title}\n\nHello."

    pipeline = FallbackTrackingPipeline(
        email_generator=StopAfterFirstEmail(), tracker=MasterTracker(excel_path=workbook),
        delta_store=DeltaStore(db_path=outputs / "delta.db"),
    )
    with cancellation(event), pytest.raises(RunCancelled):
        pipeline.process_fallbacks(
            application_results_path=outputs / "missing.json", qualified_jobs_path=qualified_path,
            profile_path=profile_path, force_track_all=True,
        )

    rows = openpyxl.load_workbook(workbook).active.max_row - 1
    assert rows == 1, "the row logged before Stop must be on disk"


# ==============================================================================
# DASHBOARD RUNNER
# ==============================================================================

def test_stop_ends_a_long_phase_promptly_and_reports_cancelled():
    """End to end through the runner: Stop during a phase, not after it."""
    from job_agent.web import runner as runner_module
    from tests.test_web import _drain

    runner = runner_module.PipelineRunner()
    channel = runner.subscribe()
    started = threading.Event()

    def long_phase(options, cancel):
        started.set()
        for _ in range(600):  # would take a minute if Stop were ignored
            check_cancelled()
            time.sleep(0.1)
        return {"ok": True}

    original = runner_module._PHASE_IMPLS["source"]
    runner_module._PHASE_IMPLS["source"] = long_phase
    try:
        assert runner.start(["source", "evaluate"], {}) is None
        assert started.wait(5)
        began = time.monotonic()
        assert runner.cancel() is True
        events = _drain(channel, until="run_end", timeout=10)
        elapsed = time.monotonic() - began
    finally:
        runner_module._PHASE_IMPLS["source"] = original

    assert elapsed < 3, f"Stop took {elapsed:.1f}s; it must not wait for the phase to finish"
    ended = next(e for e in events if e["type"] == "phase_end" and e["phase"] == "source")
    assert ended["status"] == "cancelled"
    assert events[-1]["status"] == "cancelled"
    assert not any(e["type"] == "phase_start" and e["phase"] == "evaluate" for e in events)
