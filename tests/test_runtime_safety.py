import hashlib
import json
import threading
from pathlib import Path

import pytest

from job_agent.automation.pipeline import AutoApplyPipeline
from job_agent.config.settings import settings
from job_agent.runtime import pipeline_lock, invalidate_after
from tests.test_pipeline_integration import flow


def test_full_profile_seal_detects_nonmetric_changes(flow):
    profile = flow["profile"]
    assert profile.verify_integrity()
    profile.contact.full_name = "Different Person"
    assert not profile.verify_integrity()


def test_live_apply_rejects_changed_pdf_before_browser(flow):
    Path(flow["records"][0]["pdf_path"]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="PDF is missing or has changed"):
        AutoApplyPipeline(delta_store=flow["store"]).run_applications(
            manifest_path=flow["manifest_path"], profile_path=flow["profile_path"],
            qualified_jobs_path=flow["qualified_path"], assume_yes=True,
        )


def test_live_apply_requires_the_ats_pdf_audit(flow):
    pdf = Path(flow["records"][0]["pdf_path"])
    pdf.with_suffix(".ats.json").unlink()
    with pytest.raises(ValueError, match="ATS audit"):
        AutoApplyPipeline(delta_store=flow["store"]).run_applications(
            manifest_path=flow["manifest_path"], profile_path=flow["profile_path"],
            qualified_jobs_path=flow["qualified_path"], assume_yes=True,
        )


def test_live_attempt_is_claimed_once_across_repeated_batches(flow):
    calls = []

    class Agent:
        def apply_to_job(self, **kwargs):
            calls.append(kwargs)
            job = kwargs["job"]
            return dict(job_id=job.id, title=job.title, company=job.company,
                        job_url=job.job_url, status="applied", applied=True, steps_taken=1)

        def close(self):
            pass

    pipeline = AutoApplyPipeline(agent=Agent(), delta_store=flow["store"])
    kwargs = dict(manifest_path=flow["manifest_path"], profile_path=flow["profile_path"],
                  qualified_jobs_path=flow["qualified_path"],
                  output_path=flow["root"] / "application_results.json", assume_yes=True)
    assert pipeline.run_applications(**kwargs)[0][0]["status"] == "applied"
    assert pipeline.run_applications(**kwargs)[1][0]["status"] == "skipped"
    assert len(calls) == 1


def test_downstream_artifacts_are_archived_after_source(tmp_path):
    for filename in ("qualified_jobs.json", "application_results.json"):
        (tmp_path / filename).write_text("[]")
    invalidate_after("source", tmp_path)
    assert not (tmp_path / "qualified_jobs.json").exists()
    assert len(list((tmp_path / "history").rglob("*.json"))) == 2


def test_lock_is_reentrant_but_rejects_another_thread():
    errors = []

    def contender():
        try:
            with pipeline_lock():
                pass
        except RuntimeError as exc:
            errors.append(str(exc))

    with pipeline_lock():
        with pipeline_lock():
            thread = threading.Thread(target=contender)
            thread.start()
            thread.join(timeout=3)
            assert not thread.is_alive()
    assert len(errors) == 1
