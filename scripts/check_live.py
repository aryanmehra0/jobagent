"""Read-only live connection checks. Never print credentials or submit applications."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from job_agent.config.settings import settings
from job_agent.llm import groq_complete, LLMError
from job_agent.sourcing.ats_direct import ATSDirectIngestion


def main():
    report = {"provider": settings.active_provider, "model": settings.groq_model,
              "configured_keys": len(settings.groq_keys)}
    try:
        reply = groq_complete("Return a JSON object with ok equal to true.", "Connection check.", max_tokens=256)
        report["groq"] = "ok" if reply.get("ok") is True else "unexpected response"
    except LLMError as exc:
        report["groq"] = str(exc)
    ats = ATSDirectIngestion()
    data = ats._get_json("https://boards-api.greenhouse.io/v1/boards/stripe/jobs?content=true", "Greenhouse")
    report["greenhouse_jobs"] = len(data.get("jobs", [])) if isinstance(data, dict) else 0
    print(json.dumps(report, indent=2))
    return 0 if report["groq"] == "ok" and report["greenhouse_jobs"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
