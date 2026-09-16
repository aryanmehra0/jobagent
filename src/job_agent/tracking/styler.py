"""Openpyxl Excel Spreadsheet Styling Engine.

Applies professional typography, wrapped text alignments, dark navy header styling,
and priority-based PatternFill color-coding to master application tracking sheets.
"""

from __future__ import annotations

from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.utils import get_column_letter

# Header Styles
HEADER_FONT = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
HEADER_FILL = PatternFill(fill_type="solid", start_color="1E293B", end_color="1E293B")
HEADER_ALIGNMENT = Alignment(horizontal="center", vertical="center", wrap_text=True)

# Priority Score Fills (Soft pastel tones for high executive legibility)
FILL_HIGH_PRIORITY = PatternFill(fill_type="solid", start_color="D1FAE5", end_color="D1FAE5")   # Soft Green (>= 8.5)
FILL_MED_PRIORITY = PatternFill(fill_type="solid", start_color="FEF3C7", end_color="FEF3C7")    # Soft Yellow (7.0 - 8.4)
FILL_LOW_PRIORITY = PatternFill(fill_type="solid", start_color="FFE4E6", end_color="FFE4E6")    # Soft Red (< 7.0)

# Cell Alignments
ALIGN_LEFT_TOP = Alignment(horizontal="left", vertical="top")
ALIGN_CENTER_TOP = Alignment(horizontal="center", vertical="top")
ALIGN_WRAP_TEXT = Alignment(horizontal="left", vertical="top", wrap_text=True)

# Gridlines
THIN_BORDER = Border(
    left=Side(style="thin", color="E2E8F0"),
    right=Side(style="thin", color="E2E8F0"),
    top=Side(style="thin", color="E2E8F0"),
    bottom=Side(style="thin", color="E2E8F0"),
)


def get_priority_fill(score: float) -> PatternFill:
    """Return color-coded PatternFill based on semantic fit score."""
    if score >= 8.5:
        return FILL_HIGH_PRIORITY
    elif score >= 7.0:
        return FILL_MED_PRIORITY
    return FILL_LOW_PRIORITY


def style_header_row(ws: Worksheet) -> None:
    """Apply styling to row 1 headers."""
    ws.row_dimensions[1].height = 28
    for col_idx in range(1, ws.max_column + 1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = HEADER_ALIGNMENT
        cell.border = THIN_BORDER


# Columns holding wrapped multi-line prose, sized to the wrap width rather than
# to their longest line (a full cold email would otherwise demand a 2000px column).
WRAPPED_TEXT_COLUMNS = (7, 8)


def autofit_column_widths(ws: Worksheet, min_width: int = 12, max_width: int = 65) -> None:
    """Size each column to its content, leaving hidden columns alone."""
    for col in ws.columns:
        if not col:
            continue
        col_letter = get_column_letter(col[0].column)
        # A hidden column (the bookkeeping job-ID column) must stay hidden.
        if ws.column_dimensions[col_letter].hidden:
            continue

        if col[0].column in WRAPPED_TEXT_COLUMNS:
            ws.column_dimensions[col_letter].width = max_width
            continue

        max_len = max((len(str(cell.value or "")) for cell in col), default=0)
        ws.column_dimensions[col_letter].width = max(min_width, min(max_len + 3, max_width))

