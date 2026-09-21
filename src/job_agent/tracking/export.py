"""Cumulative CSV sheet of every job the agent has found.

`jobs_master.csv` holds one row per job, keyed by job ID, and is rebuilt from the
pipeline's artifacts after each phase: sourcing adds rows, evaluation adds the
fit score, tailoring the resume, and applying the outcome. A row is updated in
place, never duplicated, and a value already recorded is not blanked by a later
phase that does not know it — each sweep only writes its own new jobs, so
earlier jobs would otherwise lose their details.

The file is UTF-8 with a byte-order mark so Excel opens company names such as
"Zoho Corporation Pvt. Ltd — Chennai" without mangling them.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from rich.console import Console

from job_agent.config.schema import JobPosting
from job_agent.config.settings import settings

console = Console()

JOBS_CSV_NAME = "jobs_master.csv"

COLUMNS = [
    "Job ID", "Date Found", "Title", "Company", "Location", "Work Mode", "Remote", "Source", "Posted",
    "Salary Min", "Salary Max", "Currency", "Fit Score", "Status",
    "HR / Careers Email", "Email Type", "Email Source", "Email Found On", "Other Emails",
    "Company Website", "Job URL", "Apply URL", "Apply Method", "Auto-apply Possible",
    "Tailored Resume", "Open Resume", "Tailored Resume Path", "Resume Check", "Outreach To", "Outreach Status", "Cold Email Subject", "Cold Email Body",
    "Email Draft File", "Notes", "Target Country", "Resume Format", "Remote Eligibility",
    "Eligibility Notes", "Email Verification", "Matched Skills", "Missing Skills", "Search Batch",
    "Last Seen In Search", "Search Data", "Posting Freshness", "Application Readiness", "Next Step",
]

_SOURCE_LABELS = {"job_post": "Job post", "company_site": "Company website", "hunter": "Hunter.io"}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def job_row(job: JobPosting) -> Dict[str, str]:
    """The sourcing columns for one job."""
    from job_agent.automation.routing import route_application

    route = route_application(job)
    primary = job.primary_contact()
    others = [contact.email for contact in job.contacts if primary is None or contact.email != primary.email]
    return {
        "Job ID": job.id,
        "Date Found": (job.discovered_at or "")[:10],
        "Title": job.title,
        "Company": job.company,
        "Location": job.location or "",
        "Work Mode": (job.work_mode or "onsite").title(),
        "Remote": "Yes" if job.is_remote else "No",
        "Source": job.source or "",
        "Posted": str(job.date_posted or "")[:10],
        "Salary Min": "" if job.salary_min is None else f"{job.salary_min:g}",
        "Salary Max": "" if job.salary_max is None else f"{job.salary_max:g}",
        # The schema defaults the currency; it means nothing without a salary.
        "Currency": (job.salary_currency or "") if job.salary_min or job.salary_max else "",
        "HR / Careers Email": primary.email if primary else "",
        "Email Type": primary.kind if primary else "",
        "Email Source": _SOURCE_LABELS.get(primary.source, primary.source) if primary else "",
        "Email Found On": (primary.source_url or (job.job_url if primary.source == "job_post" else "")) if primary else "",
        "Email Verification": "Published / deliverability not checked" if primary and primary.source != "hunter" else (
            "Provider supplied / deliverability not checked" if primary else "No published email found"),
        "Other Emails": "; ".join(others),
        "Company Website": job.company_website or "",
        "Job URL": job.job_url,
        "Apply URL": route.url or "",
        "Apply Method": route.channel.replace("_", " "),
        "Auto-apply Possible": "Yes" if route.automatable else "No - " + route.reason,
        "Status": "found",
    }


def _promote_status(current: str, new: str) -> str:
    """Keep the furthest lifecycle stage, so re-sourcing does not reset "applied"."""
    from job_agent.tracking.records import promote_status

    return promote_status(current, new)


class JobsCsvExporter:
    """Builds and upserts the cumulative jobs CSV."""

    def __init__(self, csv_path: Optional[Path] = None, outputs_dir: Optional[Path] = None):
        self.csv_path = Path(csv_path) if csv_path else (settings.outputs_dir / JOBS_CSV_NAME)
        self.outputs_dir = Path(outputs_dir) if outputs_dir else settings.outputs_dir

    def load(self) -> Dict[str, Dict[str, str]]:
        """Existing rows keyed by job ID."""
        if not self.csv_path.is_file():
            return {}
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            return {row["Job ID"]: row for row in csv.DictReader(handle) if row.get("Job ID")}

    @staticmethod
    def _merge(rows: Dict[str, Dict[str, str]], job_id: str, values: Dict[str, str]) -> None:
        row = rows.setdefault(job_id, {column: "" for column in COLUMNS})
        for column, value in values.items():
            if column == "Status":
                row["Status"] = _promote_status(row.get("Status", ""), value)
            elif value not in (None, ""):
                row[column] = str(value)

    def export(self) -> Path:
        """Update the CSV from the current artifacts and return its path."""
        from datetime import date

        from job_agent.tracking.records import collect_records

        rows = self.load()
        for row in rows.values():
            row["Search Batch"] = "Earlier search; fit may be stale"
        today = date.today().isoformat()
        from job_agent.tailoring.regional import regional_policy, remote_eligibility
        profile = _read_json(settings.profile_path, {})
        current_country = profile.get("work_authorization", {}).get("current_country")
        latest_file = self.outputs_dir / "latest_jobs.json"
        latest_ids = {job.get("id") for job in _read_json(latest_file, []) if isinstance(job, dict)}
        coverage = _read_json(self.outputs_dir / "source_coverage.json", {})

        for record in collect_records(self.outputs_dir).values():
            values = job_row(record.job)
            if record.id in rows:
                # First discovery date wins; a re-scraped copy has a newer stamp.
                values.pop("Date Found")
            else:
                values["Date Found"] = values["Date Found"] or today
            values["Status"] = record.status
            values["Search Batch"] = "Current search"
            if latest_file.exists():
                values["Search Batch"] = "Current search" if record.id in latest_ids else "Pending from earlier search"
            if record.id in latest_ids:
                values["Last Seen In Search"] = coverage.get("checked_at", "")
                feed = coverage.get("public_feeds", {}).get(record.job.source, {})
                values["Search Data"] = "Cached feed; see source coverage" if feed.get("cached_requests") else "Fetched during latest search"
            age = record.job.age_hours()
            values["Posting Freshness"] = "Posting date unknown" if age is None else (
                "Within search window" if age <= coverage.get("hours_old", 48) else "Older than search window")
            policy = regional_policy(record.job)
            values["Target Country"] = policy["country"]
            values["Remote Eligibility"], values["Eligibility Notes"] = remote_eligibility(record.job, current_country)
            values["Matched Skills"] = "; ".join(record.evaluation.get("matching_skills") or [])
            values["Missing Skills"] = "; ".join(record.evaluation.get("missing_skills") or [])
            if record.fit_score is not None:
                values["Fit Score"] = f"{record.fit_score:.1f}"
            values["Tailored Resume"] = record.tailored_resume or ""
            values["Resume Check"] = record.resume_check or ""
            resume_file = self.outputs_dir / "tailored_resumes" / (record.tailored_resume or "")
            values["Tailored Resume Path"] = str(resume_file) if record.tailored_resume and resume_file.is_file() else ""
            # Excel evaluates this when the sheet is opened: one click opens the PDF.
            values["Open Resume"] = (
                f'=HYPERLINK("{resume_file.resolve()}","Open {record.tailored_resume}")'
                if values["Tailored Resume Path"] else ""
            )
            values["Notes"] = record.notes or ""
            if record.apply_url:
                values["Apply URL"] = record.apply_url
            self._merge(rows, record.id, values)

            draft = record.outreach
            if draft:
                row = rows[record.id]
                # Outreach state is current, not cumulative: a hold can become ready.
                row["Outreach To"] = draft.get("to", "")
                row["Outreach Status"] = draft.get("note", "")
                row["Cold Email Subject"] = draft.get("subject", "")
                row["Cold Email Body"] = draft.get("body", "")
                row["Email Draft File"] = Path(draft["eml"]).name if draft.get("eml") else ""

        self._refresh_resume_columns(rows)
        self._refresh_readiness(rows, profile.get("profile_hash"))
        self._write(rows.values())
        latest_export = JobsCsvExporter(csv_path=self.csv_path.parent / "jobs_latest.csv", outputs_dir=self.outputs_dir)
        latest_export._write(row for row in rows.values() if row.get("Search Batch") == "Current search")
        ready_export = JobsCsvExporter(csv_path=self.csv_path.parent / "applications_ready.csv", outputs_dir=self.outputs_dir)
        ready_export._write(row for row in rows.values() if row.get("Application Readiness") == "Ready for your review")
        return self.csv_path

    def _refresh_readiness(self, rows, profile_hash):
        import hashlib
        manifest = {item.get("job_id"): item for item in _read_json(self.outputs_dir / "tailored_resumes/manifest.json", [])
                    if isinstance(item, dict)}
        for job_id, row in rows.items():
            row["Application Readiness"] = "Needs preparation"
            if row.get("Status") == "applied":
                row["Application Readiness"] = "Already applied"
                row["Next Step"] = "Track the employer response; do not apply again."
                continue
            if row.get("Search Batch") != "Current search" or row.get("Posting Freshness") == "Older than search window":
                row["Next Step"] = "Refresh the search and check the employer listing before applying."
                continue
            record = manifest.get(job_id, {})
            pdf = (self.outputs_dir / "tailored_resumes" / f"resume_{job_id}.pdf").resolve()
            valid = False
            if (pdf.parent == (self.outputs_dir / "tailored_resumes").resolve() and pdf.is_file()
                    and profile_hash and record.get("profile_hash") == profile_hash):
                audit = _read_json(pdf.with_suffix(".ats.json"), {})
                passed = record.get("validation_passed") is True or (record.get("validation_passed") is None and audit.get("passed") is True)
                valid = passed and hashlib.sha256(pdf.read_bytes()).hexdigest() == record.get("pdf_sha256")
            if valid and row.get("Remote Eligibility") != "Location restricted":
                row["Application Readiness"] = "Ready for your review"
                row["Next Step"] = "Review the PDF, eligibility and live listing; open Apply URL to apply."
            elif not row.get("Fit Score"):
                row["Next Step"] = "Run evaluation; this job has not been scored."
            else:
                row["Next Step"] = "Review the fit score; generate and validate a tailored PDF if qualified."

    def _refresh_resume_columns(self, rows: Dict[str, Dict[str, str]]) -> None:
        """Point every row, from any sweep, at the resume file that exists for it now.

        Rows from earlier sweeps are not in the current artifacts, so without
        this they kept a stale path, no link, and no check result after their
        resumes were rebuilt.
        """
        folder = self.outputs_dir / "tailored_resumes"
        checks: Dict[str, str] = {}
        for entry in _read_json(folder / "manifest.json", []):
            if isinstance(entry, dict) and entry.get("job_id"):
                checks[entry["job_id"]] = entry.get("validation_summary") or "generated from profile (integrity gate)"
                if entry["job_id"] in rows:
                    rows[entry["job_id"]]["Resume Format"] = (
                        entry.get("regional", {}).get("paper") or entry.get("mode") or "generated")
                    if entry.get("target_country"):
                        rows[entry["job_id"]]["Target Country"] = entry["target_country"]
                if entry["job_id"] not in rows:
                    # A job whose resume exists but which predates this sheet.
                    score = entry.get("score")
                    rows[entry["job_id"]] = {column: "" for column in COLUMNS} | {
                        "Job ID": entry["job_id"],
                        "Date Found": str(entry.get("tailored_at") or "")[:10],
                        "Title": entry.get("title") or "",
                        "Company": entry.get("company") or "",
                        "Fit Score": "" if score is None else f"{float(score):.1f}",
                        "Status": "tailored",
                    }
        for job_id, row in rows.items():
            row["Open Resume"] = ""
            pdf = folder / f"resume_{job_id}.pdf"
            if pdf.is_file():
                row["Tailored Resume"] = pdf.name
                row["Tailored Resume Path"] = str(pdf.resolve())
                row["Open Resume"] = f'=HYPERLINK("{pdf.resolve()}","Open {pdf.name}")'
                if job_id in checks:
                    row["Resume Check"] = checks[job_id]
            elif row.get("Tailored Resume"):
                row["Tailored Resume Path"] = ""
                row["Open Resume"] = ""
                row["Resume Check"] = "resume file no longer exists - run: python main.py tailor --rebuild-existing"

    def _write(self, rows: Iterable[Dict[str, str]]) -> None:
        def rank(row):
            try:
                score = float(row.get("Fit Score") or -1)
            except ValueError:
                score = -1
            return (score, row.get("Date Found", ""))
        ordered: List[Dict[str, str]] = sorted(rows, key=rank, reverse=True)
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.csv_path.with_suffix(".csv.tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in ordered:
                writer.writerow({column: spreadsheet_text(row.get(column, ""))
                                 if column != "Open Resume" else row.get(column, "") for column in COLUMNS})
        try:
            temporary.replace(self.csv_path)
        except PermissionError:
            temporary.unlink(missing_ok=True)
            raise PermissionError(
                f"Could not write {self.csv_path}. It is probably open in Excel; close it and export again."
            )


def spreadsheet_text(value: Any) -> str:
    """Prevent untrusted listing text from becoming spreadsheet formulas."""
    text = str(value or "")
    if text.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


def export_jobs_csv(quiet: bool = False) -> Optional[Path]:
    """Refresh the jobs CSV, reporting rather than raising on failure.

    Called after each phase; a sheet left open in Excel must not fail the phase
    whose real work has already been saved.
    """
    try:
        path = JobsCsvExporter().export()
    except Exception as exc:
        console.print(f"[yellow]Jobs CSV not updated: {exc}[/yellow]")
        return None
    if not quiet:
        console.print(f"[bold green]Jobs sheet updated:[/bold green] [yellow]{path}[/yellow]")
    return path
