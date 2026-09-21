"""Bounded checks against public ATS APIs and two JobSpy boards."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from job_agent.sourcing.ats_direct import ATSDirectIngestion
from job_agent.config.settings import settings


def main():
    feed = ATSDirectIngestion(timeout=20)
    report = {}
    for name, fetch, token in [("greenhouse", feed.fetch_greenhouse_jobs, "stripe"),
                               ("lever", feed.fetch_lever_jobs, "ramp"),
                               ("ashby", feed.fetch_ashby_jobs, "linear")]:
        jobs = fetch(token)
        report[name] = {"jobs": len(jobs), "distinct_ids": len({j.id for j in jobs})}
        print(name, report[name], flush=True)
    from jobspy import scrape_jobs
    for board in ("linkedin", "indeed"):
        try:
            jobs = scrape_jobs(site_name=[board], search_term="AI Product Manager",
                               location="United States", country_indeed="usa",
                               results_wanted=3, hours_old=168, verbose=0)
            report[board] = {"jobs": len(jobs)}
        except Exception as exc:
            report[board] = {"error_type": type(exc).__name__}
        print(board, report[board], flush=True)
    destination = settings.outputs_dir / "live_sources_report.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
