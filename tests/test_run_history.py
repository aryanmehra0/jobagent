"""Every run is kept: when it ran, for whose profile, on which resume, and the jobs it found."""
from __future__ import annotations

import json
import threading
import time

from job_agent.config.schema import JobPosting
from job_agent.config.settings import settings
from job_agent.sourcing import details
from job_agent.storage.jobs_db import JobsDatabase, list_runs, run_job_ids
from job_agent.web import runner as run
from tests.test_pipeline_integration import flow  # noqa: F401  (fixture)


def _profile(tmp_path, name="Asha Verma"):
    path = tmp_path / "profile.json"
    path.write_text(json.dumps({"contact": {"full_name": name}, "profile_hash": "abc123"}), encoding="utf-8")
    return path


def _quiet_runner(monkeypatch, tmp_path, name="Asha Verma"):
    from job_agent import workflow
    monkeypatch.setattr(run, "build_snapshot", lambda: {})
    monkeypatch.setattr(workflow, "publish_outputs", lambda **kw: {"files": {}, "warnings": []})
    monkeypatch.setattr(settings, "profile_path", _profile(tmp_path, name))


def test_every_run_gets_an_identity_a_profile_and_its_jobs(monkeypatch, tmp_path):
    _quiet_runner(monkeypatch, tmp_path)
    settings.outputs_dir.mkdir(parents=True, exist_ok=True)
    (settings.outputs_dir / "latest_jobs.json").write_text(json.dumps([{"id": "j1"}, {"id": "j2"}, {"id": "j3"}]))
    monkeypatch.setitem(run._PHASE_IMPLS, "source", lambda o, c: {"latest_matches": 12, "new_jobs": 3})

    result = run.PipelineRunner().run_sync(["source"], {"dry_run": True, "resume": "cv.pdf", "tailoring_mode": "auto"})

    runs = list_runs()
    assert len(runs) == 1
    record = runs[0]
    assert record["run_id"] == result["id"] and record["run_id"][:2] == "20"
    assert record["candidate_name"] == "Asha Verma" and record["profile_hash"] == "abc123"
    assert record["resume_file"] == "cv.pdf" and record["dry_run"] is True
    assert record["status"] == "ok" and record["finished_at"] and record["started_at"] < record["finished_at"]
    assert record["results"]["source"]["status"] == "ok"
    assert record["totals"]["jobs_found"] == 12
    assert record["job_count"] == 3 and sorted(run_job_ids(record["run_id"])) == ["j1", "j2", "j3"]


def test_phase_history_rows_now_carry_the_run_they_belong_to(monkeypatch, tmp_path):
    _quiet_runner(monkeypatch, tmp_path)
    monkeypatch.setitem(run._PHASE_IMPLS, "source", lambda o, c: {"latest_matches": 1})
    result = run.PipelineRunner().run_sync(["source"], {})
    with JobsDatabase()._connect() as conn:
        ids = [row["run_id"] for row in conn.execute("SELECT run_id FROM phase_runs").fetchall()]
    assert ids and set(ids) == {result["id"]}, "run_id used to be blank on every row"


def test_a_failed_run_is_kept_with_its_error(monkeypatch, tmp_path):
    _quiet_runner(monkeypatch, tmp_path)

    def boom(options, cancel):
        raise RuntimeError("source exploded")

    monkeypatch.setitem(run._PHASE_IMPLS, "source", boom)
    run.PipelineRunner().run_sync(["source"], {})
    record = list_runs()[0]
    assert record["status"] == "error"
    assert "exploded" in record["results"]["source"]["summary"]["error"]


def test_runs_can_be_listed_per_profile_newest_first(tmp_path):
    database = JobsDatabase(tmp_path / "jobs.db")
    for run_id, name, when in (("a", "Asha", "2026-10-01T09:00:00"), ("b", "Ravi", "2026-10-02T09:00:00"),
                               ("c", "Asha", "2026-10-03T09:00:00")):
        database.save_run({"run_id": run_id, "started_at": when, "status": "ok", "candidate_name": name,
                           "phases": ["source"], "results": {}, "dry_run": True})
    assert [r["run_id"] for r in database.list_runs()] == ["c", "b", "a"]
    assert [r["run_id"] for r in database.list_runs(candidate="asha")] == ["c", "a"]  # key; legacy rows fall back to the lowercased name


def test_saving_a_run_twice_updates_it_instead_of_duplicating(tmp_path):
    database = JobsDatabase(tmp_path / "jobs.db")
    database.save_run({"run_id": "r1", "started_at": "2026-10-01T09:00:00", "status": "running",
                       "candidate_name": "Asha"})
    database.save_run({"run_id": "r1", "started_at": "2026-10-01T09:00:00", "status": "ok", "candidate_name": "Asha",
                       "finished_at": "2026-10-01T09:30:00", "totals": {"scored": 4}})
    runs = database.list_runs()
    assert len(runs) == 1 and runs[0]["status"] == "ok" and runs[0]["totals"] == {"scored": 4}
    database.link_run_jobs("r1", ["x", "y", "x"])
    database.link_run_jobs("r1", ["y", "z"])
    assert sorted(database.run_job_ids("r1")) == ["x", "y", "z"]


# --- Fetching missing descriptions -------------------------------------------

def _listing(index, source="indeed", description=""):
    return JobPosting(id=f"j{index}", title=f"AI Engineer {index}", company="Acme",
                      job_url=f"https://acme.example/jobs/{index}", source=source, description=description)


def test_missing_descriptions_are_fetched_in_parallel_and_the_rest_left_alone(monkeypatch):
    threads = set()

    def fake_fetch(job, session, proxy=None):
        threads.add(threading.get_ident())
        time.sleep(0.05)
        return "" if job.id == "j3" else f"Build and ship ML systems for {job.company}. " * 4

    monkeypatch.setattr(details, "_fetch_description", fake_fetch)
    jobs = [_listing(i) for i in range(8)] + [_listing(99, description="Already has a real description. " * 5)]
    started = time.perf_counter()
    result, report = details.enrich_missing_descriptions(jobs, workers=4)
    elapsed = time.perf_counter() - started

    assert report == {"requested": 8, "fetched": 7, "unavailable": 1}
    assert [job.id for job in result] == [job.id for job in jobs]
    assert result[0].description.startswith("Build and ship") and result[3].description == ""
    assert result[8].description == jobs[8].description, "a listing that already has one is not refetched"
    assert len(threads) > 1 and elapsed < 8 * 0.05 * 0.8, "fetches should overlap"


def test_a_linkedin_listing_without_a_real_view_url_is_not_fetched(monkeypatch):
    called = []
    monkeypatch.setattr(details, "_fetch_description",
                        lambda job, session, proxy=None: called.append(job.id) or "text " * 30)
    bad = JobPosting(id="li", title="AI Engineer", company="Acme", source="linkedin",
                     job_url="https://www.linkedin.com/jobs/search/?keywords=ai")
    result, report = details.enrich_missing_descriptions([bad])
    assert called == [] and report["fetched"] == 0 and report["unavailable"] == 1


# --- Resumes for every job must never widen what is applied to automatically ---

def test_only_qualified_jobs_are_ever_applied_to_even_when_every_job_has_a_resume(flow):
    from tests.test_pipeline_integration import apply

    manifest_path = flow["manifest_path"]
    entries = json.loads(manifest_path.read_text(encoding="utf-8"))
    qualified_id = entries[0]["job_id"]
    # A resume made for a job that did not qualify, as `tailor_jobs` now produces.
    entries.append({**entries[0], "job_id": "weak-match", "title": "Unrelated Role"})
    manifest_path.write_text(json.dumps(entries), encoding="utf-8")

    successful, failed = apply(flow)

    touched = {item["job_id"] for item in successful + failed}
    assert touched == {qualified_id}, "an unqualified job must not be applied to"


# --- A different candidate's profile must be able to re-score earlier jobs ----

def test_a_new_profile_reopens_jobs_judged_for_the_previous_candidate(tmp_path):
    from job_agent.sourcing.delta_store import DeltaStore

    store = DeltaStore(tmp_path / "delta.db")
    jobs = [_listing(i, description="d " * 50) for i in range(4)]
    store.mark_many_seen(jobs)
    for job, status in zip(jobs, ("evaluated_rejected", "qualified", "prefilter_rejected", "applied")):
        store.update_status(job.id, status)
    assert store.filter_unseen(jobs) == [], "all four are seen before the profile changes"

    assert store.reopen_for_new_profile() == 3, "applied jobs are history and are not reopened"
    unseen = {job.id for job in store.filter_unseen(jobs)}
    assert unseen == {"j0", "j1", "j2"}

    # Found again by a sweep, a reopened job goes back to being an ordinary scraped one.
    store.mark_many_seen([jobs[0]])
    assert store.statuses(["j0"]) == {"j0": "scraped"}
    assert {job.id for job in store.filter_unseen(jobs)} == {"j1", "j2"}, "j0 is seen again for the new profile"


def test_reopening_is_idempotent_and_leaves_an_untouched_store_alone(tmp_path):
    from job_agent.sourcing.delta_store import DeltaStore

    store = DeltaStore(tmp_path / "delta.db")
    assert store.reopen_for_new_profile() == 0
    store.mark_many_seen([_listing(1)])
    store.update_status("j1", "evaluated_rejected")
    assert store.reopen_for_new_profile() == 1
    assert store.reopen_for_new_profile() == 0


def test_profile_identity_prefers_email_and_survives_a_missing_file(tmp_path):
    from job_agent.intake.parser import _profile_identity

    path = tmp_path / "profile.json"
    assert _profile_identity(path) is None
    path.write_text(json.dumps({"contact": {"full_name": "Asha Verma", "email": "Asha@Example.org"}}))
    assert _profile_identity(path) == "asha@example.org"
    path.write_text(json.dumps({"contact": {"full_name": "Asha Verma"}}))
    assert _profile_identity(path) == "asha verma"


def test_a_sweep_reopens_jobs_when_the_profile_belongs_to_someone_else_now(tmp_path):
    from job_agent.sourcing.delta_store import DeltaStore

    store = DeltaStore(tmp_path / "delta.db")
    jobs = [_listing(i) for i in range(3)]
    store.mark_many_seen(jobs)
    for job in jobs:
        store.update_status(job.id, "evaluated_rejected")

    assert store.note_profile("asha@example.org") == 0, "the first call only records whose profile this is"
    assert store.note_profile("asha@example.org") == 0
    assert store.filter_unseen(jobs) == []

    assert store.note_profile("ravi@example.org") == 3, "a different candidate: everything judged for Asha reopens"
    assert {job.id for job in store.filter_unseen(jobs)} == {"j0", "j1", "j2"}
    assert store.note_profile("ravi@example.org") == 0


def test_an_unreadable_profile_never_reopens_anything(tmp_path):
    from job_agent.sourcing.delta_store import DeltaStore

    store = DeltaStore(tmp_path / "delta.db")
    store.mark_many_seen([_listing(1)])
    store.update_status("j1", "evaluated_rejected")
    store.note_profile("asha@example.org")
    assert store.note_profile(None) == 0


# --- A run whose process died must not stay "running" for ever -----------------

def test_a_run_that_lost_its_process_is_closed_as_interrupted(monkeypatch, tmp_path):
    _quiet_runner(monkeypatch, tmp_path)
    database = JobsDatabase()
    database.save_run({"run_id": "dead-run", "started_at": "2026-10-01T09:00:00", "status": "running",
                       "candidate_name": "Asha Verma"})
    monkeypatch.setitem(run._PHASE_IMPLS, "source", lambda o, c: {"latest_matches": 1})

    result = run.PipelineRunner().run_sync(["source"], {})

    statuses = {r["run_id"]: r["status"] for r in list_runs()}
    assert statuses["dead-run"] == "interrupted"
    assert statuses[result["id"]] == "ok", "the run that is alive must not be touched"


def test_closing_orphans_never_touches_finished_runs(tmp_path):
    database = JobsDatabase(tmp_path / "jobs.db")
    for run_id, status in (("a", "ok"), ("b", "error"), ("c", "running")):
        database.save_run({"run_id": run_id, "started_at": "2026-10-01T09:00:00", "status": status})
    assert database.close_orphaned_runs() == 1
    assert {r["run_id"]: r["status"] for r in database.list_runs()} == {"a": "ok", "b": "error", "c": "interrupted"}
    assert database.close_orphaned_runs() == 0


def test_a_run_without_a_search_still_lists_the_jobs_it_scored_and_tailored(monkeypatch, tmp_path):
    _quiet_runner(monkeypatch, tmp_path)
    out = settings.outputs_dir
    (out / "tailored_resumes").mkdir(parents=True, exist_ok=True)
    (out / "evaluated_jobs.json").write_text(json.dumps([{"job": {"id": "e1"}}, {"job": {"id": "e2"}}]))
    (out / "tailored_resumes" / "manifest.json").write_text(json.dumps([{"job_id": "e1"}, {"job_id": "t9"}]))
    monkeypatch.setitem(run._PHASE_IMPLS, "evaluate", lambda o, c: {"scored": 2})
    monkeypatch.setitem(run._PHASE_IMPLS, "tailor", lambda o, c: {"compiled": 2})

    result = run.PipelineRunner().run_sync(["evaluate", "tailor"], {})

    assert sorted(run_job_ids(result["id"])) == ["e1", "e2", "t9"], "the union, without duplicates"
