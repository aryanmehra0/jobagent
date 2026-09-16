"""Column-aware text extraction for PDF resumes.

`page.extract_text()` reads strictly top-to-bottom, which is correct for a
single-column document and wrong for a two-column one. A sidebar template
interleaves the two columns line by line, producing text like::

    CONTACT Sofia Marino
    sofia.marino@example.com Frontend Engineer
    JavaScript (cid:127) Reduced bundle size by 41%

Every downstream stage then sees skills spliced into job bullets and section
headings buried mid-line. This module detects the vertical gutter between
columns from word coordinates and emits each column as a contiguous block, so
the section parser sees the document a human would read.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# A gutter must be at least this wide (in points) to count as a column break,
# which keeps ordinary inter-word spacing from splitting a single column.
MIN_GUTTER_WIDTH = 14.0

# The split must fall within the middle of the page. A "gutter" at 5% is a margin.
MIN_SPLIT_RATIO = 0.22
MAX_SPLIT_RATIO = 0.78

# Each side must hold at least this share of the page's words, otherwise the gap
# is whitespace around a heading rather than a real column.
MIN_COLUMN_SHARE = 0.18

# A real column is many lines tall. A right-aligned date on each role header also
# leaves a clean vertical gutter, but only spans a handful of lines.
MIN_COLUMN_LINES = 5

# If nearly every line of the narrower side sits on the same baseline as a line of
# the wider side, the two are cells of one row — a right-aligned date beside its
# employer — not independent columns. Splitting those would strand every date at
# the end of the document, detached from the role it belongs to.
MAX_ROW_ALIGNMENT = 0.8

# Words whose vertical centres differ by less than this belong to the same line.
LINE_TOLERANCE = 3.0


def _group_words_into_lines(words: Sequence[Dict[str, Any]]) -> List[str]:
    """Reassemble positioned words into text lines, top to bottom.

    Words are bucketed by vertical position and then ordered left to right, which
    is what turns a column's scattered word boxes back into readable lines.
    """
    if not words:
        return []

    ordered = sorted(words, key=lambda word: (round(word["top"], 1), word["x0"]))
    lines: List[List[Dict[str, Any]]] = []
    for word in ordered:
        if lines and abs(word["top"] - lines[-1][0]["top"]) <= LINE_TOLERANCE:
            lines[-1].append(word)
        else:
            lines.append([word])

    rendered: List[str] = []
    for line in lines:
        line.sort(key=lambda word: word["x0"])
        rendered.append(" ".join(word["text"] for word in line).strip())
    return [line for line in rendered if line]


def find_column_split(words: Sequence[Dict[str, Any]], page_width: float) -> Optional[float]:
    """Find the x coordinate of a two-column gutter, or None for a single column.

    Works by marking every horizontal band covered by a word, then looking for the
    widest uncovered band near the middle of the page.
    """
    if not words or page_width <= 0:
        return None

    # One bucket per point of page width; resumes are never wide enough for this
    # to be expensive, and it keeps the scan exact rather than approximate.
    covered = [False] * (int(page_width) + 1)
    for word in words:
        start = max(0, int(word["x0"]))
        end = min(len(covered) - 1, int(word["x1"]))
        for index in range(start, end + 1):
            covered[index] = True

    low = int(page_width * MIN_SPLIT_RATIO)
    high = int(page_width * MAX_SPLIT_RATIO)

    best_start = best_length = 0
    run_start: Optional[int] = None
    for index in range(low, high + 1):
        if not covered[index]:
            if run_start is None:
                run_start = index
            length = index - run_start + 1
            if length > best_length:
                best_start, best_length = run_start, length
        else:
            run_start = None

    if best_length < MIN_GUTTER_WIDTH:
        return None

    split = best_start + best_length / 2.0

    left_words = [word for word in words if word["x1"] <= split]
    right_words = [word for word in words if word["x0"] >= split]

    # Both sides must carry real content, or this is just a wide margin.
    if min(len(left_words), len(right_words)) < len(words) * MIN_COLUMN_SHARE:
        return None

    left_lines = _line_positions(left_words)
    right_lines = _line_positions(right_words)
    if min(len(left_lines), len(right_lines)) < MIN_COLUMN_LINES:
        return None

    # Reject cells of a shared row masquerading as a column.
    narrow, wide = (
        (right_lines, left_lines) if len(right_lines) <= len(left_lines) else (left_lines, right_lines)
    )
    aligned = sum(
        1 for position in narrow
        if any(abs(position - other) <= LINE_TOLERANCE for other in wide)
    )
    if narrow and aligned / len(narrow) > MAX_ROW_ALIGNMENT:
        return None

    return split


def _line_positions(words: Sequence[Dict[str, Any]]) -> List[float]:
    """Distinct vertical baselines occupied by a set of words."""
    positions: List[float] = []
    for top in sorted(word["top"] for word in words):
        if not positions or abs(top - positions[-1]) > LINE_TOLERANCE:
            positions.append(top)
    return positions


def extract_page_lines(page: Any) -> List[str]:
    """Extract one page as ordered text lines, splitting columns when present.

    Falls back to the page's own `extract_text()` whenever word positions are
    unavailable, so a page this cannot analyse still yields its text.
    """
    try:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    except Exception:
        words = []

    if not words:
        text = page.extract_text() or ""
        return [line for line in text.splitlines() if line.strip()]

    split = find_column_split(words, float(page.width or 0))
    if split is None:
        return _group_words_into_lines(words)

    left_words = [word for word in words if word["x1"] <= split]
    right_words = [word for word in words if word["x0"] >= split]
    # Words straddling the gutter belong to whichever side holds more of them.
    for word in words:
        if word["x1"] > split > word["x0"]:
            if (split - word["x0"]) >= (word["x1"] - split):
                left_words.append(word)
            else:
                right_words.append(word)

    # The denser column goes first. It holds the candidate's name and the main
    # sections; leading with a narrow sidebar would put the name *after* the
    # sidebar's own headings, so it would be read as part of that section
    # instead of as the document header.
    first, second = (
        (left_words, right_words)
        if len(left_words) >= len(right_words)
        else (right_words, left_words)
    )
    return _group_words_into_lines(first) + _group_words_into_lines(second)


def describe_layout(page: Any) -> Tuple[str, Optional[float]]:
    """Report a page's layout as ("single-column"|"two-column", split_x).

    Used by the readiness report so a candidate is told their template is
    two-column, which is the usual explanation for a jumbled extraction.
    """
    try:
        words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    except Exception:
        return "unknown", None
    if not words:
        return "no-text", None
    split = find_column_split(words, float(page.width or 0))
    return ("two-column", split) if split else ("single-column", None)
