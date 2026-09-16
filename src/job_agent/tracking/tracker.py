"""Master application tracking spreadsheet engine (openpyxl).

Creates and maintains `applications_tracker.xlsx`: one styled row per job, with
the match score, lifecycle status, direct link, failure reason, synthesized cold
outreach email, and the tailored resume filename.

The workbook is the human-facing artifact of the whole pipeline, so two properties
matter: a job appears at most once (re-running `track` updates its row rather than
appending a duplicate), and the sheet is saved once per batch rather than once per
row, which keeps a 200-job run from taking minutes.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import openpyxl
from openpyxl.styles import Font
from openpyxl.worksheet.worksheet import Worksheet
from rich.console import Console

from job_agent.config.schema import JobPosting
from job_agent.config.settings import settings
from job_agent.tracking.styler import (
    ALIGN_CENTER_TOP,
    ALIGN_LEFT_TOP,
    ALIGN_WRAP_TEXT,
    THIN_BORDER,
    autofit_column_widths,
    get_priority_fill,
    style_header_row,
)

console = Console()

COLUMNS = [
    "Date Found",
    "Job Title",
    "Company",
    "Match Score",
    "Status",
    "Direct Link",
    "Failure Reason / Notes",
    "Personalized Cold Outreach Email",
    "Tailored Resume PDF",
]

# Hidden column holding the job ID, which is what makes a row updatable.
JOB_ID_COLUMN = len(COLUMNS) + 1

SHEET_TITLE = "Applications & Outreach"


class MasterTracker:
    """Manages the persistent application tracking workbook."""

    def __init__(self, excel_path: Optional[Path] = None):
        self.excel_path = Path(excel_path) if excel_path else settings.tracker_path
        self.excel_path.parent.mkdir(parents=True, exist_ok=True)
        self.wb: openpyxl.Workbook
        self.ws: Worksheet
        self._dirty = False
        self._load_or_create_workbook()

    # --- Workbook lifecycle ---------------------------------------------------

    def _load_or_create_workbook(self) -> None:
        """Open the existing workbook, or initialize a new one with styled headers."""
        if self.excel_path.exists():
            try:
                self.wb = openpyxl.load_workbook(str(self.excel_path))
                self.ws = self.wb.active
                self._ensure_header()
                return
            except Exception as exc:
                console.print(
                    f"[yellow]Could not open the existing workbook ({exc}); starting a fresh sheet. "
                    f"The old file is left untouched at {self.excel_path}.[/yellow]"
                )

        self.wb = openpyxl.Workbook()
        self.ws = self.wb.active
        self.ws.title = SHEET_TITLE
        self._write_header()
        self.save()

    def _write_header(self) -> None:
        """Write and style the header row, including the hidden job-ID column."""
        for index, name in enumerate(COLUMNS, start=1):
            self.ws.cell(row=1, column=index, value=name)
        self.ws.cell(row=1, column=JOB_ID_COLUMN, value="Job ID")
        style_header_row(self.ws)
        self._hide_id_column()

    def _hide_id_column(self) -> None:
        """Hide the job-ID column; it is bookkeeping, not something to read."""
        letter = self.ws.cell(row=1, column=JOB_ID_COLUMN).column_letter
        self.ws.column_dimensions[letter].hidden = True

    def _ensure_header(self) -> None:
        """Add the job-ID column to a workbook created before it existed."""
        if self.ws.cell(row=1, column=JOB_ID_COLUMN).value != "Job ID":
            self.ws.cell(row=1, column=JOB_ID_COLUMN, value="Job ID")
            self._dirty = True
        # Applied every time: an older workbook was saved without the hidden flag.
        self._hide_id_column()

    def _row_index_by_job_id(self) -> Dict[str, int]:
        """Map each tracked job ID to the row that holds it."""
        index: Dict[str, int] = {}
        for row in range(2, self.ws.max_row + 1):
            job_id = self.ws.cell(row=row, column=JOB_ID_COLUMN).value
            if job_id:
                index[str(job_id)] = row
        return index

    def save(self) -> None:
        """Write the workbook to disk, resizing columns first."""
        autofit_column_widths(self.ws)
        try:
            self.wb.save(str(self.excel_path))
        except PermissionError:
            raise PermissionError(
                f"Could not write {self.excel_path}. The file is probably open in Excel; "
                "close it and run the command again."
            )
        self._dirty = False

    # --- Row writing ----------------------------------------------------------

    def log_application(
        self,
        job: JobPosting,
        match_score: float,
        status: str,
        cold_email: str,
        failure_reason: Optional[str] = None,
        pdf_path: Optional[str] = None,
        autosave: bool = True,
    ) -> int:
        """Insert or update this job's row and return its row number.

        Args:
            autosave: Save immediately. Batch callers pass False and call `save()`
                once at the end, since saving an xlsx is O(sheet size).
        """
        existing = self._row_index_by_job_id()
        row_index = existing.get(job.id, self.ws.max_row + 1)
        is_update = job.id in existing

        values = [
            datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            job.title,
            job.company,
            f"{match_score:.1f}",
            status.upper(),
            job.job_url,
            failure_reason or "N/A",
            cold_email,
            Path(pdf_path).name if pdf_path else "N/A",
        ]

        for column, value in enumerate(values, start=1):
            cell = self.ws.cell(row=row_index, column=column, value=value)
            cell.border = THIN_BORDER
            if column in (1, 4, 5):
                cell.alignment = ALIGN_CENTER_TOP
            elif column in (7, 8):
                cell.alignment = ALIGN_WRAP_TEXT
            else:
                cell.alignment = ALIGN_LEFT_TOP

            if column == 6 and job.job_url:
                cell.hyperlink = job.job_url
                cell.font = Font(color="2563EB", underline="single")

        self.ws.cell(row=row_index, column=JOB_ID_COLUMN, value=job.id)

        priority_fill = get_priority_fill(match_score)
        self.ws.cell(row=row_index, column=4).fill = priority_fill
        self.ws.cell(row=row_index, column=5).fill = priority_fill
        self.ws.row_dimensions[row_index].height = 110

        self._dirty = True
        if autosave:
            self.save()

        verb = "Updated" if is_update else "Logged"
        console.print(
            f"[bold green]{verb}[/bold green] [cyan]{job.title} @ {job.company}[/cyan] (row {row_index})"
        )
        return row_index
