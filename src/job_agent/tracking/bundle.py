"""Portable, auditable application pack with relative CSV and HTML links."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import json
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from job_agent.config.settings import settings
from job_agent.runtime import exclusive_run
from job_agent.tracking.export import COLUMNS, JobsCsvExporter, _read_json, spreadsheet_text


def portable_workbook(rows):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    workbook = Workbook()
    workbook.remove(workbook.active)
    groups = [('Ready for review', [r for r in rows if r.get('Application Readiness') == 'Ready for your review' and r.get('Tailored Resume')]),
              ('Latest search', [r for r in rows if r.get('Search Batch') == 'Current search']), ('All saved jobs', rows)]
    for title, subset in groups:
        sheet = workbook.create_sheet(title)
        sheet.append(COLUMNS)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        for row in subset:
            sheet.append([spreadsheet_text(row.get(col, '')) for col in COLUMNS])
        sheet.freeze_panes = 'A2'
        sheet.auto_filter.ref = sheet.dimensions
        for column in ('C', 'D', 'E'):
            sheet.column_dimensions[column].width = 28
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def pack_index(rows, coverage, table_rows):
    escape = html.escape
    cards = []
    ready = [row for row in rows if row.get('Application Readiness') == 'Ready for your review' and row.get('Tailored Resume')]
    for row in ready:
        documents = ''.join(f'<a download href="{escape(row[col], quote=True)}">{label}</a>' for col, label in
                            [('Tailored Resume', 'Resume PDF'), ('Cover Letter', 'Cover letter PDF'), ('Interview Prep', 'Interview guide')]
                            if row.get(col))
        url = row.get('Apply URL') or row.get('Job URL') or ''
        apply = f'<a href="{escape(url, quote=True)}" target="_blank" rel="noopener noreferrer">Open employer listing</a>' if url.startswith(('https://', 'http://')) else ''
        cards.append(f'<article><h3>{escape(row.get("Title", ""))}</h3><p>{escape(row.get("Company", ""))} · {escape(row.get("Location", ""))}</p>'
                     f'<p>Fit: {escape(row.get("Fit Score", ""))}/10 · {escape(row.get("Remote Eligibility", "Review eligibility"))}</p>'
                     f'<nav>{documents}{apply}</nav><p>{escape(row.get("HR / Careers Email") or "No published email; use the application link.")}</p></article>')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Start here — your application pack</title><style>'
            'body{font:16px/1.6 system-ui;margin:0;background:#f5f7fb;color:#17253d}main{max-width:1120px;margin:auto;padding:28px}'
            'nav{display:flex;gap:12px;flex-wrap:wrap}a{color:#155ac0}nav a{background:#eaf0ff;border-radius:8px;padding:10px 14px}'
            'article{background:white;border:1px solid #dde3ee;border-radius:12px;padding:22px;margin:18px 0}'
            '.table{overflow:auto}table{border-collapse:collapse;min-width:800px}td,th{padding:10px;text-align:left;border-bottom:1px solid #ddd}'
            '</style></head><body><main><h1>Your application pack</h1>'
            '<ol><li>Extract the entire ZIP into a folder.</li><li>Review a job below, then open its matching resume and optional cover letter.</li>'
            '<li>Check the live listing and eligibility, apply yourself, then mark it applied in the dashboard.</li></ol>'
            f'<p>Last saved search: {escape(str(coverage.get("checked_at") or "Not recorded"))}. Downloading this pack does not run a new search.</p>'
            '<nav><a download href="applications_ready.csv">Application shortlist CSV</a><a download href="jobs_latest.csv">Latest search CSV</a>'
            '<a download href="jobs.csv">All saved jobs CSV</a><a download href="applications_tracker.xlsx">Excel workbook</a></nav>'
            f'<h2>Ready for your review ({len(ready)})</h2>' + (''.join(cards) or '<p>No current applications have validated PDFs. Return to the agent and run evaluation and tailoring.</p>') +
            '<p>Interview guides contain practice prompts, not verified company interview questions. Published email addresses are not deliverability verified. Creating this pack sends nothing.</p>'
            f'<details><summary>All saved jobs ({len(rows)}) — includes older searches</summary><div class="table"><table><thead><tr>'
            '<th>Role</th><th>Company</th><th>Location</th><th>Fit</th><th>Contact</th><th>Eligibility</th><th>Job</th><th>Documents</th>'
            '</tr></thead><tbody>' + ''.join(table_rows) + '</tbody></table></div></details></main></body></html>')


@exclusive_run
def build_application_pack(outputs_dir: Path | None = None) -> Path:
    out = Path(outputs_dir or settings.outputs_dir).resolve()
    exporter = JobsCsvExporter(outputs_dir=out, csv_path=out / "jobs_master.csv")
    exporter.export()
    rows = list(exporter.load().values())
    manifest = {item["job_id"]: item for item in _read_json(out / "tailored_resumes/manifest.json", [])
                if isinstance(item, dict) and item.get("job_id")}
    profile = _read_json(settings.profile_path, {})
    profile_hash = profile.get("profile_hash")
    target = out / "application_pack.zip"
    temporary = out / "application_pack.zip.tmp"
    included, skipped, links, documents = [], [], [], []
    try:
        with ZipFile(temporary, "w", ZIP_DEFLATED) as archive:
            from job_agent.tracking.supplements import document_links
            extra_documents = document_links(out, profile_hash)
            for row in rows:
                job_id = row.get("Job ID", "")
                record = manifest.get(job_id, {})
                # Never trust a path from a CSV or archived manifest outside this folder.
                pdf = (out / "tailored_resumes" / f"resume_{job_id}.pdf").resolve()
                pdf_link = ""
                if record and pdf.parent == out / "tailored_resumes" and pdf.is_file():
                    contents = pdf.read_bytes()
                    audit = _read_json(pdf.with_suffix(".ats.json"), {})
                    valid = record.get("validation_passed") is True or (
                        record.get("validation_passed") is None and audit.get("passed") is True)
                    same_profile = bool(profile_hash) and record.get("profile_hash") == profile_hash
                    digest = hashlib.sha256(contents).hexdigest()
                    if valid and same_profile and digest == record.get("pdf_sha256"):
                        pdf_link = f"resumes/{pdf.name}"
                        archive.writestr(pdf_link, contents)
                        included.append({"job_id": job_id, "file": pdf_link, "sha256": digest})
                if row.get("Tailored Resume") and not pdf_link:
                    skipped.append({"job_id": job_id, "reason": "No current, validated PDF matching this profile and manifest hash."})
                    row["Resume Check"] = "Not included in pack: no current validated PDF matching the active profile and manifest."
                row["Tailored Resume Path"] = pdf_link
                row["Open Resume"] = pdf_link
                row["Tailored Resume"] = pdf_link
                # Drafts can contain old attachments; the pack provides the current verified PDF separately.
                row["Email Draft File"] = ""
                for column, subfolder in (("Interview Prep", "interview_prep"), ("Cover Letter", "cover_letters")):
                    source = extra_documents.get(job_id, {}).get(column)
                    row[column] = ""
                    if source:
                        relative = f"{subfolder}/{Path(source).name}"
                        archive.write(source, relative)
                        documents.append({'job_id': job_id, 'file': relative, 'sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest()})
                        row[column] = relative
                escape = html.escape
                url = row.get("Apply URL") or row.get("Job URL") or ""
                apply = f'<a href="{escape(url, quote=True)}">Apply</a>' if url.startswith(("https://", "http://")) else ""
                resume = f'<a href="{escape(pdf_link)}">PDF</a>' if pdf_link else "Not included"
                links.append("<tr>" + "".join(f"<td>{escape(str(row.get(col, '')))}</td>" for col in
                             ("Title", "Company", "Location", "Fit Score", "HR / Careers Email", "Remote Eligibility"))
                             + f"<td>{apply}</td><td>{resume}" + ''.join(
                                 f'<br><a href="{escape(row[col])}">{col}</a>'
                                 for col in ('Interview Prep', 'Cover Letter') if row.get(col)) + '</td></tr>')
            sheet = io.StringIO(newline="")
            writer = csv.DictWriter(sheet, fieldnames=COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows({col: spreadsheet_text(row.get(col, "")) for col in COLUMNS} for row in rows)
            archive.writestr("jobs.csv", sheet.getvalue().encode("utf-8-sig"))
            latest_sheet = io.StringIO(newline="")
            latest_writer = csv.DictWriter(latest_sheet, fieldnames=COLUMNS, extrasaction="ignore")
            latest_writer.writeheader()
            latest_writer.writerows({col: spreadsheet_text(row.get(col, "")) for col in COLUMNS}
                                   for row in rows if row.get("Search Batch") == "Current search")
            archive.writestr("jobs_latest.csv", latest_sheet.getvalue().encode("utf-8-sig"))
            ready_sheet = io.StringIO(newline="")
            ready_writer = csv.DictWriter(ready_sheet, fieldnames=COLUMNS, extrasaction="ignore")
            ready_writer.writeheader()
            ready_writer.writerows({col: spreadsheet_text(row.get(col, "")) for col in COLUMNS}
                                  for row in rows if row.get("Application Readiness") == "Ready for your review"
                                  and row.get("Tailored Resume"))
            archive.writestr("applications_ready.csv", ready_sheet.getvalue().encode("utf-8-sig"))
            archive.writestr("index.html", pack_index(rows, _read_json(out/'source_coverage.json', {}), links))
            archive.writestr("applications_tracker.xlsx", portable_workbook(rows))
            archive.writestr("manifest.json", json.dumps({"jobs": len(rows), "pdfs": included, "documents": documents, "omitted_pdfs": skipped}, indent=2))
            archive.writestr("README.txt", "Extract all files before opening index.html or jobs.csv.\n"
                             "Start with index.html: the review shortlist and all downloads are linked there.\n"
                             "applications_tracker.xlsx contains portable Ready, Latest and History sheets.\n"
                             "Optional cover letters and interview guides are included when generated and verified.\n"
                             "CSV cannot embed attachments; the resumes folder contains the matching PDFs.\n"
                             "Only current, hash-checked PDFs for the active profile are included. See manifest.json for omissions.\n"
                             "Emails are published/provider-supplied contacts, not guaranteed recipients or deliverable inboxes.\n"
                             "No applications or emails have been sent by creating this download.\n")
            coverage = out / "source_coverage.json"
            if coverage.is_file():
                archive.write(coverage, "source_coverage.json")
            report = out / "run_report.json"
            if report.is_file():
                archive.write(report, "run_report.json")
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
