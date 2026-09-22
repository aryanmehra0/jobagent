"""The actual run contract: fresh exports, honest failures, reusable intake."""
import csv
import hashlib
import json
import threading
from zipfile import ZipFile

import pytest

from job_agent.config.normalize import utc_now_iso
from job_agent.config.schema import JobPosting, SearchParameters
from job_agent.config.settings import settings
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.sourcing.scraper import OmnichannelScraper
from job_agent.tracking.export import JobsCsvExporter
from job_agent.web import runner as run
from tests.test_tailoring import candidate_profile


def posting():
    return JobPosting(id="fresh", title="AI Engineer", company="Example AI", location="Remote",
                      job_url="https://example.ai/jobs/1", source="indeed", is_remote=True,
                      date_posted=utc_now_iso(), description="Build machine learning services in Python.")


def test_repeat_search_keeps_latest_csv_without_repeating_processing(monkeypatch, tmp_path):
    out = settings.outputs_dir
    out.mkdir(parents=True)
    scraper = OmnichannelScraper(SearchParameters(target_domains=["AI Engineer"], job_boards=["indeed"],
                                                find_contacts=False), delta_store=DeltaStore(tmp_path / "delta.db"))
    monkeypatch.setattr(scraper, "scrape_job_boards", lambda: [posting()])
    assert len(scraper.run_sourcing_pipeline(include_ats_direct=False)) == 1
    scraper.delta_store.update_status("fresh", "evaluated_rejected")
    manifest = out / "tailored_resumes/manifest.json"
    manifest.parent.mkdir()
    manifest.write_text("[]")
    assert scraper.run_sourcing_pipeline(include_ats_direct=False) == []
    assert manifest.exists(), "A no-new-jobs run must retain previous validated outputs"
    JobsCsvExporter().export()
    with (out / "jobs_latest.csv").open(encoding="utf-8-sig", newline="") as handle:
        latest = list(csv.DictReader(handle))
    assert len(latest) == 1
    assert latest[0]["Last Seen In Search"]
    assert json.loads((out / "source_coverage.json").read_text())["previously_seen"] == 1


def test_all_sources_blocked_does_not_report_success(monkeypatch, tmp_path):
    scraper = OmnichannelScraper(SearchParameters(target_domains=["AI Engineer"], job_boards=["indeed"],
                                                find_contacts=False), delta_store=DeltaStore(tmp_path / "delta.db"))
    def blocked():
        scraper.blocked_boards["indeed"] = "HTTP 403"
        return []
    monkeypatch.setattr(scraper, "scrape_job_boards", blocked)
    assert scraper.run_sourcing_pipeline(include_ats_direct=False) == []
    report = json.loads((settings.outputs_dir / "source_coverage.json").read_text())
    assert report["status"] == "failed"
    JobsCsvExporter().export()
    with (settings.outputs_dir / "jobs_latest.csv").open(encoding="utf-8-sig", newline="") as handle:
        assert list(csv.DictReader(handle)) == []


def test_failed_phase_still_exports_csv_and_pack(monkeypatch, tmp_path):
    monkeypatch.setattr(run, "build_snapshot", lambda: {})
    monkeypatch.setattr(settings, "profile_path", tmp_path / "absent-profile.json")
    def source(options, cancel):
        out = settings.outputs_dir
        (out / "scraped_jobs.json").write_text(json.dumps([posting().model_dump()]))
        raise RuntimeError("source failed after saving results")
    monkeypatch.setitem(run._PHASE_IMPLS, "source", source)
    result = run.PipelineRunner().run_sync(["source", "evaluate"], {"dry_run": True})
    assert result["status"] == "error"
    report = json.loads((settings.outputs_dir / "run_report.json").read_text())
    assert "source failed" in report["phases"]["source"]["error"]
    assert "evaluate" not in report["phases"]
    assert (settings.outputs_dir / "jobs_master.csv").is_file()
    with ZipFile(settings.outputs_dir / "application_pack.zip") as archive:
        assert "jobs_latest.csv" in archive.namelist()


def test_export_error_does_not_mask_original_phase_error(monkeypatch):
    from job_agent import workflow
    monkeypatch.setattr(run, "build_snapshot", lambda: {})
    monkeypatch.setattr(workflow, "publish_outputs", lambda **kw: {"files": {}, "warnings": ["CSV is locked by Excel"]})
    def fail(options, cancel):
        raise ValueError("original phase error")
    monkeypatch.setitem(run._PHASE_IMPLS, "source", fail)
    result = run.PipelineRunner().run_sync(["source"], {})
    assert result["status"] == "error"
    assert result["report"]["phases"]["source"]["error"] == "original phase error"
    assert "CSV is locked by Excel" in result["report"]["warnings"]


def test_warning_status_is_persisted_not_lost_by_pop(monkeypatch):
    from job_agent import workflow
    monkeypatch.setattr(run, "build_snapshot", lambda: {})
    monkeypatch.setattr(workflow, "publish_outputs", lambda **kw: {"files": {}, "warnings": []})
    monkeypatch.setitem(run._PHASE_IMPLS, "source", lambda o, c: {"_status": "warning", "found": 1})
    result = run.PipelineRunner().run_sync(["source"], {})
    assert result["status"] == "warning"
    assert result["report"]["phases"]["source"]["status"] == "warning"


def test_unchanged_resume_reuses_sealed_profile(monkeypatch, tmp_path, candidate_profile):
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"unchanged source bytes")
    profile_file = tmp_path / "profile.json"
    profile_file.write_text(candidate_profile.model_dump_json())
    profile_file.with_suffix(".source.json").write_text(json.dumps({
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "profile_hash": candidate_profile.profile_hash,
    }))
    monkeypatch.setattr(settings, "profile_path", profile_file)
    from job_agent.intake.parser import ResumeParser
    monkeypatch.setattr(ResumeParser, "parse", lambda *a, **kw: pytest.fail("Unchanged resume should not be parsed"))
    assert run._phase_intake({"resume": str(source)}, threading.Event())["reused"] is True


def test_sales_role_requires_an_explicit_sales_target():
    from job_agent.sourcing.relevance import title_matches
    assert not title_matches("Sales Engineer - AI Products", ["AI ML Engineer", "AI Product Manager"])
    assert title_matches("Sales Engineer - AI Products", ["Sales Engineer"])
    assert title_matches("AI Engineer", ["AI ML Engineer"])


def test_description_requests_skip_irrelevant_and_already_complete_jobs():
    from job_agent.sourcing.details import enrich_linkedin_details
    class Session:
        def get(self, *args, **kwargs):
            pytest.fail("A complete description must not trigger another request")
    jobs, report = enrich_linkedin_details([posting()], session=Session())
    assert len(jobs) == 1 and report["requested"] == 0


def test_manual_applied_marker_is_reversible_and_updates_downloads(monkeypatch, tmp_path):
    from job_agent.tracking.manual import mark_applied
    monkeypatch.setattr(settings, "profile_path", tmp_path / "no-profile.json")
    out = settings.outputs_dir
    out.mkdir(parents=True)
    (out / "scraped_jobs.json").write_text(json.dumps([posting().model_dump()]))
    JobsCsvExporter().export()
    mark_applied("fresh")
    assert JobsCsvExporter().load()["fresh"]["Status"] == "applied"
    assert (out / "application_pack.zip").exists()
    mark_applied("fresh", undo=True)
    assert JobsCsvExporter().load()["fresh"]["Status"] == "found"
    with pytest.raises(ValueError, match="Only your own"):
        mark_applied("fresh", undo=True)


def test_cli_full_run_uses_shared_flow_and_defaults_to_dry_run(monkeypatch, tmp_path, candidate_profile):
    from click.testing import CliRunner
    from job_agent.cli import cli
    profile = tmp_path / "profile.json"
    profile.write_text(candidate_profile.model_dump_json())
    monkeypatch.setattr(settings, "profile_path", profile)
    calls = []
    def run_sync(self, phases, options):
        calls.append((phases, options))
        return {"status": "ok", "report": {}}
    monkeypatch.setattr(run.PipelineRunner, "run_sync", run_sync)
    result = CliRunner().invoke(cli, ["run-pipeline", "--skip-intake"])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == ["source", "evaluate", "tailor", "apply", "track", "prep"]
    assert calls[0][1]["dry_run"] is True
    assert calls[0][1]["assume_yes"] is False
    assert calls[0][1]["tailoring_mode"] == "regional"


def test_cli_daily_builds_cover_letter_pack_by_default(monkeypatch, tmp_path, candidate_profile):
    from click.testing import CliRunner
    from job_agent.cli import cli
    profile = tmp_path / "profile.json"
    profile.write_text(candidate_profile.model_dump_json())
    monkeypatch.setattr(settings, "profile_path", profile)
    pack = tmp_path / "application_pack.zip"
    calls = []

    def run_sync(self, phases, options):
        calls.append((phases, options))
        return {"status": "ok", "report": {"files": {"application_pack": str(pack)}, "warnings": []}}

    monkeypatch.setattr(run.PipelineRunner, "run_sync", run_sync)
    monkeypatch.setattr("job_agent.tracking.bundle.validate_application_pack",
                        lambda path: {"jobs": 3, "resumes": 2, "supporting_documents": 4, "checked_documents": 6, "valid": True})
    result = CliRunner().invoke(cli, ["daily", "--limit", "2"])
    assert result.exit_code == 0, result.output
    assert calls[0][0] == ["source", "evaluate", "tailor", "apply", "track", "prep"]
    assert calls[0][1]["dry_run"] is True
    assert calls[0][1]["cover_letter"] is True
    assert calls[0][1]["limit"] == 2
    assert "Pack verified" in result.output


def test_cli_quality_scores_current_outputs(monkeypatch, tmp_path, candidate_profile):
    from click.testing import CliRunner
    from job_agent.cli import cli
    out = settings.outputs_dir
    out.mkdir(parents=True)
    profile = tmp_path / "profile.json"
    profile.write_text(candidate_profile.model_dump_json())
    monkeypatch.setattr(settings, "profile_path", profile)
    for name in ("jobs_master.csv", "jobs_latest.csv", "applications_ready.csv"):
        (out / name).write_text(
            "Job ID,Status,HR / Careers Email,Application Readiness\n"
            "fresh,qualified,jobs@example.com,Ready for your review\n",
            encoding="utf-8-sig",
        )
    (out / "source_coverage.json").write_text(json.dumps({"checked_at": utc_now_iso(), "hours_old": 48}))
    (out / "evaluation_progress.json").write_text(json.dumps({"scored": 1, "missing_descriptions": 0, "failed": 0}))
    monkeypatch.setattr("job_agent.tracking.quality.validate_application_pack",
                        lambda path: {"jobs": 1, "resumes": 1, "supporting_documents": 2, "checked_documents": 3, "valid": True})
    result = CliRunner().invoke(cli, ["quality"])
    assert result.exit_code == 0, result.output
    assert "Quality score" in result.output
    report = json.loads((out / "quality_report.json").read_text(encoding="utf-8"))
    assert report["grade_out_of_10"] >= 8.0
    assert report["jobs"] == {"master": 1, "latest": 1, "ready": 1}


def test_missing_descriptions_are_counted_and_never_sent_to_scoring(tmp_path, candidate_profile):
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline
    out = settings.outputs_dir
    out.mkdir(parents=True)
    profile = tmp_path / "profile.json"
    profile.write_text(candidate_profile.model_dump_json())
    jobs = out / "scraped_jobs.json"
    jobs.write_text(json.dumps([posting().model_copy(update={"description": ""}).model_dump()]))
    evaluated, qualified = SemanticEvaluationPipeline().run_evaluation(profile_path=profile, jobs_path=jobs)
    assert evaluated == qualified == []
    progress = json.loads((out / "evaluation_progress.json").read_text())
    assert progress["input_jobs"] == progress["missing_descriptions"] == 1
    assert progress["scored"] == 0 and progress["complete"]


def test_prefilter_rejection_has_a_valid_persistent_status(tmp_path, candidate_profile):
    from types import SimpleNamespace
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline
    out = settings.outputs_dir
    out.mkdir(parents=True)
    profile = tmp_path / "profile.json"
    profile.write_text(candidate_profile.model_dump_json())
    jobs = out / "scraped_jobs.json"
    jobs.write_text(json.dumps([posting().model_dump()]))
    store = DeltaStore(tmp_path / "delta.db")
    store.mark_seen(posting())
    SemanticEvaluationPipeline(embedder=SimpleNamespace(filter_and_rank=lambda **kw: []), delta_store=store).run_evaluation(profile_path=profile, jobs_path=jobs)
    assert store.statuses(["fresh"])["fresh"] == "prefilter_rejected"


def test_stopped_worker_report_is_shown_as_interrupted():
    from job_agent.web.state import run_report_state
    settings.outputs_dir.mkdir(parents=True)
    (settings.outputs_dir / "run_report.json").write_text(json.dumps({"status": "running", "active_phase": "source"}))
    assert run_report_state()["status"] == "interrupted"
    from job_agent.runtime import pipeline_lock
    with pipeline_lock():
        assert run_report_state()["status"] == "running"
