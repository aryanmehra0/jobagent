"""Live Groq + public ATS validation, isolated from the candidate's working data.

Uses the bundled fictional resume, real public jobs, real PDFs and Excel.
Application execution is always dry-run. Results are retained for inspection.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from job_agent.config.settings import settings
from job_agent.config.schema import SearchParameters
from job_agent.intake.parser import ResumeParser
from job_agent.sourcing.ats_direct import ATSDirectIngestion
from job_agent.sourcing.scraper import OmnichannelScraper
from job_agent.sourcing.delta_store import DeltaStore
from job_agent.evaluation.pipeline import SemanticEvaluationPipeline
from job_agent.evaluation.embedder import SemanticEmbedder
from job_agent.tailoring.pipeline import ResumeTailoringPipeline
from job_agent.automation.pipeline import AutoApplyPipeline
from job_agent.tracking.pipeline import FallbackTrackingPipeline


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-run", type=Path, help="Resume tailoring/tracking from an isolated validation run")
    args = parser.parse_args()
    validation_root = (settings.outputs_dir / "live_validation").resolve()
    root = args.resume_run.resolve() if args.resume_run else validation_root / datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    if validation_root not in root.parents:
        raise ValueError("Validation output must be inside data/outputs/live_validation.")
    root.mkdir(parents=True, exist_ok=True)
    settings.outputs_dir = root
    settings.profile_path = root / "profile.json"
    settings.tracker_path = root / "applications_tracker.xlsx"
    settings.llm_strict = True

    # Isolate resume resolution too, not just outputs/profile: tailoring's
    # demo-vs-real-resume guard (tailoring/pipeline.py's _faithful_source)
    # calls choose_resume(), which by default scans the shared
    # data/raw_resumes/ directory. Without this, the guard correctly detects
    # the operator's own real resume sitting there and refuses to tailor from
    # the sample -- meaning this script could only validate Phase 4 on a
    # machine that had never actually been used, which defeats the point of
    # a live validation run. Point it at an isolated copy of just the sample
    # resume so this script is self-contained regardless of real usage.
    isolated_resumes = root / "raw_resumes"
    isolated_resumes.mkdir(parents=True, exist_ok=True)
    isolated_sample = isolated_resumes / "sample_resume.pdf"
    if not isolated_sample.exists():
        isolated_sample.write_bytes((settings.raw_resumes_dir / "sample_resume.pdf").read_bytes())
    settings.raw_resumes_dir = isolated_resumes
    report = {"directory": str(root), "provider": settings.active_provider, "model": settings.groq_model}
    try:
        store = DeltaStore()
        if args.resume_run:
            report.update(json.loads((root / "validation.json").read_text(encoding="utf-8")))
            report.pop("error", None)
            report["resumed"] = True
            qualified = json.loads((root / "qualified_jobs.json").read_text(encoding="utf-8"))
        else:
            profile = ResumeParser().parse(settings.raw_resumes_dir / "sample_resume.pdf")
            report["intake"] = profile.extraction_method
            params = SearchParameters(target_domains=["Infrastructure", "Backend", "Platform"],
                                      is_remote=False, hours_old=8760,
                                      ats_companies={"greenhouse": ["stripe"]})
            feeder = ATSDirectIngestion()
            jobs = feeder.scrape_configured_ats(companies=params.ats_companies, search_params=params)
            scraper = OmnichannelScraper(search_params=params, delta_store=store)
            scraper.scrape_job_boards = lambda: jobs
            jobs = scraper.run_sourcing_pipeline(include_ats_direct=False)
            report["sourced"] = len(jobs)
            embedder = SemanticEmbedder()
            evaluated, qualified = SemanticEvaluationPipeline(embedder=embedder, delta_store=store).run_evaluation(limit=5)
            report["scored_by"] = [item.evaluation.scored_by for item in evaluated]
            report["qualified"] = len(qualified)
        if not qualified:
            raise RuntimeError("No real listings qualified; downstream validation not performed.")
        records = ResumeTailoringPipeline(delta_store=store).run_tailoring(limit=1)
        report["tailored"] = len(records)
        successful, failed = AutoApplyPipeline(delta_store=store).run_applications(dry_run=True)
        report["simulated"] = len(successful)
        report["failures"] = len(failed)
        logged = FallbackTrackingPipeline(delta_store=store).process_fallbacks(force_track_all=True)
        report["tracked"] = len(logged)
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = str(exc)
    (root / "validation.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
