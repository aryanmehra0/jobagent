"""Tailor the candidate's own resume PDF without retyping a word of it.

Rebuilding a resume from an extracted profile loses whatever the profile did not
capture. On a real resume that was the headline, the LinkedIn, GitHub and
portfolio links, the relocation line, every role's descriptor, the dates as
written ("Mar 2024", not "2024-03"), coursework, both publications, the project
roles, the skill groups, the bold lead-ins and the one-page length. One bullet
had also been split at a line wrap and its halves separated.

This module works on the original PDF instead:

1. **Read** its layout: sections, entries, bullets and their wrapped lines, from
   the positions and fonts of the text itself.
2. **Plan**: for one job description, order the bullets within each role, the
   projects within their section and the skill lines, most relevant first.
   Nothing is added, removed or reworded, and roles stay in date order.
3. **Build**: move whole blocks. Each block is copied from a version of the page
   where everything outside it has been deleted, so the original embedded fonts
   draw the original glyphs, and every word exists exactly once in the text an
   applicant tracking system reads. Unchanged pages are copied byte for byte.
4. **Validate** the result against the original: same pages, same words the same
   number of times (for two different text extractors), every bullet whole,
   every link kept with its text, the planned order visible, and no pixel
   changed outside the regions that moved. A result that fails any check is
   replaced by an exact copy of the original resume.

Layouts it cannot move safely (two columns inside a region, drawings that span
blocks, a bullet crossing a page) are left in their original order rather than
risked.
"""

from __future__ import annotations

import io
import re
import shutil
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import fitz

from job_agent.config.schema import JobPosting

BULLET_CHARS = "•●▪◦‣∙·■□➢➤►▶-–—*"

# Which sections may be reordered, and how.
_BULLET_SECTIONS = re.compile(r"EXPERIENCE|EMPLOYMENT|WORK|INTERNSHIP|LEADERSHIP|POSITIONS?\b|SKILL|COMPETENC|TECHNOLOG|TOOLS", re.I)
_ENTRY_SECTIONS = re.compile(r"PROJECT", re.I)

LINE_TOLERANCE = 1.8
MAX_PIXEL_DIFF_OUTSIDE = 0.0005


# ==============================================================================
# LAYOUT
# ==============================================================================

@dataclass
class Line:
    page: int
    top: float
    bottom: float
    x0: float
    x1: float
    baseline: float
    text: str
    bold_start: bool
    size: float
    text_x: float  # where the words start, after any bullet glyph
    is_bullet: bool
    span_ids: List[int] = field(default_factory=list)


@dataclass
class Bullet:
    lines: List[Line]

    @property
    def text(self) -> str:
        first = self.lines[0].text.lstrip()
        if first and first[0] in BULLET_CHARS:
            first = first[1:].lstrip()
        return _normalize(" ".join([first] + [line.text for line in self.lines[1:]]))


@dataclass
class Entry:
    header: List[Line]
    bullets: List[Bullet] = field(default_factory=list)

    @property
    def lines(self) -> List[Line]:
        return self.header + [line for bullet in self.bullets for line in bullet.lines]

    @property
    def title(self) -> str:
        return _normalize(" ".join(line.text for line in self.header)) if self.header else ""

    @property
    def text(self) -> str:
        return _normalize(" ".join([self.title] + [bullet.text for bullet in self.bullets]))


@dataclass
class Section:
    heading: Line
    entries: List[Entry] = field(default_factory=list)

    @property
    def title(self) -> str:
        return _normalize(self.heading.text)


@dataclass
class Layout:
    path: Path
    page_count: int
    lines: List[Line]
    sections: List[Section]
    spans: List[dict]  # every non-blank span, with its page


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


_BULLET_OR_SPACE = re.compile(r"[\s" + re.escape(BULLET_CHARS.replace("-", "").replace("–", "").replace("—", "")) + r"]")


def _compact(text: str) -> str:
    """Text with whitespace and bullet glyphs removed, for continuity checks."""
    return _BULLET_OR_SPACE.sub("", text or "")


def read_layout(pdf_path: Path) -> Layout:
    """Sections, entries and bullets of a resume PDF, from its text positions."""
    doc = fitz.open(str(pdf_path))
    spans: List[dict] = []
    for page_number, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            for raw_line in block.get("lines", []):
                for span in raw_line["spans"]:
                    if span["text"].strip():
                        spans.append({**span, "page": page_number})
    page_count = len(doc)
    page_width = doc[0].rect.width if page_count else 612.0
    doc.close()

    # Visual lines: spans sharing a baseline, left to right.
    spans.sort(key=lambda s: (s["page"], s["origin"][1], s["bbox"][0]))
    groups: List[List[int]] = []
    for index, span in enumerate(spans):
        if groups:
            last = spans[groups[-1][0]]
            if last["page"] == span["page"] and abs(last["origin"][1] - span["origin"][1]) <= LINE_TOLERANCE:
                groups[-1].append(index)
                continue
        groups.append([index])

    char_sizes: Counter = Counter()
    for span in spans:
        char_sizes[round(span["size"], 1)] += len(span["text"].strip())
    body_size = char_sizes.most_common(1)[0][0] if char_sizes else 10.0

    lines: List[Line] = []
    for group in groups:
        members = sorted((spans[i] for i in group), key=lambda s: s["bbox"][0])
        # Separate spans with a visible gap (a right-aligned date) by a space.
        text = members[0]["text"]
        for previous, span in zip(members, members[1:]):
            gap = span["bbox"][0] - previous["bbox"][2]
            if gap > 2.0 and not text.endswith(" ") and not span["text"].startswith(" "):
                text += " "
            text += span["text"]
        first = members[0]
        is_bullet = text.strip()[:1] in BULLET_CHARS and (
            len(first["text"].strip()) == 1 or first["text"].strip()[1:2] in (" ", "")
        )
        words = members[1:] if is_bullet and len(first["text"].strip()) == 1 and len(members) > 1 else members
        leading = words[0]
        lines.append(Line(
            page=first["page"],
            top=min(s["bbox"][1] for s in members),
            bottom=max(s["bbox"][3] for s in members),
            x0=min(s["bbox"][0] for s in members),
            x1=max(s["bbox"][2] for s in members),
            baseline=first["origin"][1],
            text=text,
            bold_start=bool(leading["flags"] & 16) or "bold" in leading["font"].lower(),
            size=max(s["size"] for s in members),
            text_x=leading["bbox"][0],
            is_bullet=is_bullet,
            span_ids=list(group),
        ))
    lines.sort(key=lambda line: (line.page, line.top, line.x0))

    sections: List[Section] = []
    current_bullet: Optional[Bullet] = None
    for line in lines:
        stripped = line.text.strip()
        letters = [ch for ch in stripped if ch.isalpha()]
        is_heading = (
            not line.is_bullet and letters and len(stripped) <= 48
            and (
                (line.bold_start and stripped.upper() == stripped and len(letters) >= 4)
                or line.size >= body_size + 1.5
            )
        )
        if is_heading:
            sections.append(Section(heading=line))
            current_bullet = None
            continue
        if not sections:
            continue  # the name and contact block above the first heading
        section = sections[-1]

        if not line.is_bullet and line.x0 > page_width * 0.5:
            # Text starting in the right half is a second column or a sidebar,
            # not part of this section's structure. It stays in `lines`, where
            # the safety checks see it and refuse to move blocks beside it.
            continue

        if line.is_bullet:
            if not section.entries:
                section.entries.append(Entry(header=[]))
            current_bullet = Bullet(lines=[line])
            section.entries[-1].bullets.append(current_bullet)
        elif current_bullet is not None and line.page == current_bullet.lines[-1].page \
                and abs(line.x0 - current_bullet.lines[0].text_x) <= 6.0:
            current_bullet.lines.append(line)  # a wrapped continuation of the bullet
        elif section.entries and not section.entries[-1].bullets and section.entries[-1].header \
                and not line.bold_start:
            section.entries[-1].header.append(line)  # a second header line, or paragraph text
            current_bullet = None
        else:
            section.entries.append(Entry(header=[line]))
            current_bullet = None

    return Layout(path=Path(pdf_path), page_count=page_count, lines=lines, sections=sections, spans=spans)


# ==============================================================================
# PLANNING
# ==============================================================================

@dataclass
class Unit:
    """A block that moves as one: a bullet, or a whole entry."""

    label: str
    text: str
    lines: List[Line]
    band: Optional[fitz.Rect] = None

    @property
    def page(self) -> int:
        return self.lines[0].page


@dataclass
class Group:
    """Blocks that may be reordered among themselves."""

    description: str
    units: List[Unit]
    order: List[int]
    region: Optional[fitz.Rect] = None

    @property
    def changed(self) -> bool:
        return self.order != list(range(len(self.units)))


@dataclass
class Plan:
    groups: List[Group]
    skipped: List[str]

    @property
    def moving(self) -> List[Group]:
        return [group for group in self.groups if group.changed]

    def changes(self) -> List[str]:
        notes = []
        for group in self.moving:
            first = group.units[group.order[0]]
            notes.append(f"{group.description}: now led by \"{_shorten(first.label)}\"")
        return notes


def _shorten(text: str, limit: int = 60) -> str:
    text = _normalize(text)
    lead = text.split(":")[0] if ":" in text[:60] else text
    return lead if len(lead) <= limit else lead[: limit - 1] + "…"


def plan_for_job(layout: Layout, job: JobPosting) -> Plan:
    """Most relevant blocks first, within each group that may be reordered."""
    from job_agent.tailoring.rewriter import job_terms, relevance

    terms = job_terms(job)
    groups: List[Group] = []
    skipped: List[str] = []

    def order_by_relevance(units: List[Unit]) -> List[int]:
        scored = [(-relevance(unit.text, terms), index) for index, unit in enumerate(units)]
        return [index for _, index in sorted(scored)]

    for section in layout.sections:
        title = section.title
        if _ENTRY_SECTIONS.search(title):
            entries = [entry for entry in section.entries if entry.header]
            if len(entries) >= 2:
                units = [Unit(entry.title, entry.text, entry.lines) for entry in entries]
                groups.append(Group(f"{title.title()}", units, order_by_relevance(units)))
        elif _BULLET_SECTIONS.search(title):
            for entry in section.entries:
                if len(entry.bullets) < 2:
                    continue
                units = [Unit(bullet.text, bullet.text, bullet.lines) for bullet in entry.bullets]
                name = entry.title.split("|")[0].strip() or title.title()
                groups.append(Group(_shorten(name, 70), units, order_by_relevance(units)))
    return Plan(groups=groups, skipped=skipped)


# ==============================================================================
# GEOMETRY AND SAFETY
# ==============================================================================

def _assign_bands(layout: Layout, group: Group, page_rect: fitz.Rect, drawings: List[fitz.Rect],
                  images: List[fitz.Rect]) -> Optional[str]:
    """Give each unit a band that tiles the group's region; return a reason if unsafe."""
    units = group.units
    pages = {line.page for unit in units for line in unit.lines}
    if len(pages) != 1:
        return "crosses a page break"

    page = next(iter(pages))
    page_lines = [line for line in layout.lines if line.page == page]
    unit_ids = {id(line) for unit in units for line in unit.lines}
    tops = [min(line.top for line in unit.lines) for unit in units]
    bottoms = [max(line.bottom for line in unit.lines) for unit in units]
    if any(tops[i + 1] < bottoms[i] - 3.0 for i in range(len(units) - 1)):
        return "blocks overlap"

    above = [line.bottom for line in page_lines if id(line) not in unit_ids and line.top < tops[0]]
    below = [line.top for line in page_lines if id(line) not in unit_ids and line.top >= bottoms[-1] - 1.0]
    first_boundary = (max(above) + tops[0]) / 2 if above else tops[0] - 1.0
    gap = (tops[1] - bottoms[0]) if len(units) > 1 else 2.0
    last_boundary = (bottoms[-1] + min(below)) / 2 if below else bottoms[-1] + max(gap / 2, 1.0)
    first_boundary = max(first_boundary, tops[0] - 6.0)
    last_boundary = min(last_boundary, bottoms[-1] + 6.0)

    boundaries = [first_boundary] + [(bottoms[i] + tops[i + 1]) / 2 for i in range(len(units) - 1)] + [last_boundary]
    left = page_rect.x0
    right = page_rect.x1
    for unit, (y0, y1) in zip(units, zip(boundaries, boundaries[1:])):
        unit.band = fitz.Rect(left, y0, right, y1)
    group.region = fitz.Rect(left, boundaries[0], right, boundaries[-1])

    # Every piece of text inside the region must belong to the group.
    for line in page_lines:
        if id(line) in unit_ids:
            continue
        centre = (line.top + line.bottom) / 2
        if group.region.y0 < centre < group.region.y1:
            return "other text sits beside these blocks (a column layout)"
    for rect in drawings:
        if rect.intersects(group.region) and not any(_inside(rect, unit.band) for unit in units):
            if rect.y0 >= group.region.y0 - 0.5 and rect.y1 <= group.region.y1 + 0.5 and rect.height < 0.8:
                return "a rule is drawn between these blocks"
            if not (rect.y1 <= group.region.y0 + 0.5 or rect.y0 >= group.region.y1 - 0.5):
                return "a drawing spans several blocks"
    for rect in images:
        if rect.intersects(group.region):
            return "an image sits in this region"
    return None


def _inside(rect: fitz.Rect, band: Optional[fitz.Rect]) -> bool:
    return band is not None and rect.y0 >= band.y0 - 0.25 and rect.y1 <= band.y1 + 0.25


# ==============================================================================
# BUILDING
# ==============================================================================

def _isolated(source: Path, page_number: int, band: fitz.Rect, page_rect: fitz.Rect,
              keep_line_art: bool) -> fitz.Document:
    """A copy of one page holding only the text inside `band` (and, if asked, its drawings)."""
    doc = fitz.open(str(source))
    page = doc[page_number]
    for rect in (fitz.Rect(page_rect.x0, page_rect.y0, page_rect.x1, band.y0),
                 fitz.Rect(page_rect.x0, band.y1, page_rect.x1, page_rect.y1)):
        if rect.height > 0:
            page.add_redact_annot(rect)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_REMOVE,
                          graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    if not keep_line_art:
        page.add_redact_annot(band)
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    return doc


def _background(source: Path, page_number: int, moved: Sequence[fitz.Rect]) -> fitz.Document:
    """The page with all text removed, and the drawings inside moved regions removed."""
    doc = fitz.open(str(source))
    page = doc[page_number]
    page.add_redact_annot(page.rect)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    for rect in moved:
        page.add_redact_annot(rect)
    if moved:
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_NONE)
    return doc


def build(layout: Layout, plan: Plan, output: Path) -> List[str]:
    """Write the reordered PDF; returns the reasons any group was left in place.

    A changed page is drawn as one text-free background followed by horizontal
    strips from top to bottom, each strip holding only its own text. The order
    text is written in is therefore the order it is read, on screen and by an
    applicant tracking system that reads the content stream in sequence.
    Drawing moved blocks last instead filed a role's bullets under the last
    section of the page.
    """
    source = layout.path
    src = fitz.open(str(source))
    left_in_place: List[str] = []

    moves: Dict[int, List[Group]] = {}
    for group in plan.moving:
        page = src[group.units[0].page]
        drawings = [fitz.Rect(d["rect"]) for d in page.get_drawings()]
        images = [fitz.Rect(info["bbox"]) for info in page.get_image_info()]
        reason = _assign_bands(layout, group, page.rect, drawings, images)
        if reason:
            left_in_place.append(f"{group.description}: kept in original order ({reason})")
            group.order = list(range(len(group.units)))
            continue
        moves.setdefault(group.units[0].page, []).append(group)

    out = fitz.open()
    for page_number, page in enumerate(src):
        groups = sorted(moves.get(page_number, []), key=lambda g: g.region.y0)
        if not groups:
            out.insert_pdf(src, from_page=page_number, to_page=page_number)
            continue
        rect = page.rect
        new_page = out.new_page(width=rect.width, height=rect.height)
        new_page.show_pdf_page(new_page.rect, _background(source, page_number, [g.region for g in groups]),
                               page_number)

        shifts: List[Tuple[fitz.Rect, float]] = []
        cursor = rect.y0
        for group in groups + [None]:
            static_end = group.region.y0 if group is not None else rect.y1
            if static_end - cursor > 0.01:
                strip = fitz.Rect(rect.x0, cursor, rect.x1, static_end)
                new_page.show_pdf_page(strip, _isolated(source, page_number, strip, rect, keep_line_art=False),
                                       page_number, clip=strip)
            if group is None:
                break
            y = group.region.y0
            for index in group.order:
                band = group.units[index].band
                target = fitz.Rect(band.x0, y, band.x1, y + band.height)
                new_page.show_pdf_page(target, _isolated(source, page_number, band, rect, keep_line_art=True),
                                       page_number, clip=band)
                shifts.append((band, y - band.y0))
                y += band.height
            cursor = group.region.y1

        for link in page.get_links():
            area = fitz.Rect(link["from"])
            centre_y = (area.y0 + area.y1) / 2
            for band, delta in shifts:
                if band.y0 <= centre_y < band.y1:
                    area = fitz.Rect(area.x0, area.y0 + delta, area.x1, area.y1 + delta)
                    break
            entry = {"kind": link["kind"], "from": area}
            for key in ("uri", "page", "to", "file", "zoom"):
                if key in link:
                    entry[key] = link[key]
            new_page.insert_link(entry)

    out.set_metadata({**(src.metadata or {}), "producer": "job-agent faithful tailoring"})
    output.parent.mkdir(parents=True, exist_ok=True)
    out.save(str(output), garbage=3, deflate=True)
    out.close()
    src.close()
    return left_in_place


# ==============================================================================
# VALIDATION
# ==============================================================================

def _pypdf_words(path: Path) -> Counter:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return Counter(word for page in reader.pages for word in (page.extract_text() or "").split())


def _mupdf_words(path: Path) -> Counter:
    doc = fitz.open(str(path))
    words = Counter(w[4] for page in doc for w in page.get_text("words"))
    doc.close()
    return words


def _link_signature(path: Path) -> Counter:
    doc = fitz.open(str(path))
    found = Counter()
    for page in doc:
        for link in page.get_links():
            anchor = _normalize(page.get_textbox(fitz.Rect(link["from"])))
            found[(link.get("uri") or str(link.get("page", "")), anchor)] += 1
    doc.close()
    return found


def _pypdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return " ".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def _reading_sequence(layout: Layout, plan: Plan) -> List[Tuple[str, str]]:
    """What a reader should meet, in order: headings, entry titles, then each block."""
    unit_of: Dict[int, Tuple[Group, int]] = {}
    for group in plan.groups:
        for position, unit in enumerate(group.units):
            unit_of[id(unit.lines[0])] = (group, position)

    sequence: List[Tuple[str, str]] = []

    def add(label: str) -> None:
        snippet = _compact(label)[:32]
        if len(snippet) >= 6:
            sequence.append((label, snippet))

    def in_group_order(items, first_line):
        located = [unit_of.get(id(first_line(item))) for item in items]
        if not items or any(entry is None for entry in located) or len({id(g) for g, _ in located}) != 1:
            return items
        group = located[0][0]
        by_position = {position: item for item, (_, position) in zip(items, located)}
        return [by_position[index] for index in group.order if index in by_position]

    for section in layout.sections:
        add(section.title)
        for entry in in_group_order(section.entries, lambda e: e.lines[0]):
            if entry.header:
                add(entry.title)
            for bullet in in_group_order(entry.bullets, lambda b: b.lines[0]):
                add(bullet.text)
    return sequence


def validate(layout: Layout, plan: Plan, output: Path) -> Dict[str, object]:
    """Check a tailored PDF against the original; every check must pass."""
    source = layout.path
    checks: Dict[str, Dict[str, object]] = {}

    def record(name: str, passed: bool, detail: str) -> None:
        checks[name] = {"passed": bool(passed), "detail": detail}

    src = fitz.open(str(source))
    out = fitz.open(str(output))
    record("page_count", len(src) == len(out), f"{len(src)} page(s) in the original, {len(out)} tailored")
    same_size = len(src) == len(out) and all(
        abs(a.rect.width - b.rect.width) < 0.5 and abs(a.rect.height - b.rect.height) < 0.5 for a, b in zip(src, out))
    record("page_size", same_size, "page size unchanged" if same_size else "page size differs")

    for name, reader in (("words_mupdf", _mupdf_words), ("words_pypdf", _pypdf_words)):
        before, after = reader(source), reader(output)
        missing = before - after
        extra = after - before
        detail = "every word present exactly as often as in the original"
        if missing or extra:
            detail = (f"missing {sum(missing.values())} (e.g. {', '.join(list(missing)[:4])}); "
                      f"added {sum(extra.values())} (e.g. {', '.join(list(extra)[:4])})")
        record(name, not missing and not extra, detail)

    output_text = _normalize(" ".join(page.get_text("text", sort=True) for page in out))
    compact_output = _compact(output_text)
    broken = [unit.text for group in plan.groups for unit in group.units
              if len(unit.lines) > 1 and _compact(unit.text) not in compact_output]
    record("blocks_intact", not broken,
           "every bullet and entry reads as one continuous block" if not broken
           else f"{len(broken)} block(s) broken, e.g. \"{_shorten(broken[0])}\"")

    links_before, links_after = _link_signature(source), _link_signature(output)
    record("links", links_before == links_after,
           f"{sum(links_before.values())} link(s) kept with their text" if links_before == links_after
           else f"links differ: expected {sum(links_before.values())}, found {sum(links_after.values())}")

    order_ok, order_detail = True, "planned order visible on the page"
    for group in plan.moving:
        positions = []
        for index in group.order:
            snippet = _compact(group.units[index].text)[:40]
            positions.append(compact_output.find(snippet))
        if -1 in positions or positions != sorted(positions):
            order_ok, order_detail = False, f"{group.description}: blocks not in the planned order"
            break
    record("order", order_ok, order_detail)

    expected = _reading_sequence(layout, plan)
    for name, text in (("reading_order_pypdf", _pypdf_text(output)),
                       ("reading_order_mupdf", " ".join(page.get_text("text") for page in out))):
        compact = _compact(text)
        cursor, problem = 0, None
        for label, snippet in expected:
            found = compact.find(snippet, cursor)
            if found < 0:
                problem = f"\"{_shorten(label)}\" is read out of place"
                break
            cursor = found + len(snippet)
        record(name, problem is None,
               "sections, roles and bullets are read in the order they appear" if problem is None else problem)

    moved_regions: Dict[int, List[fitz.Rect]] = {}
    for group in plan.moving:
        if group.region is not None:
            moved_regions.setdefault(group.units[0].page, []).append(group.region)
    worst = 0.0
    for page_number, (a, b) in enumerate(zip(src, out)):
        pa, pb = a.get_pixmap(dpi=72), b.get_pixmap(dpi=72)
        if (pa.width, pa.height) != (pb.width, pb.height):
            worst = 1.0
            break
        rows = [(int(r.y0) - 1, int(r.y1) + 2) for r in moved_regions.get(page_number, [])]
        stride = pa.width * pa.n
        differing = total = 0
        for row in range(pa.height):
            if any(y0 <= row <= y1 for y0, y1 in rows):
                continue
            start = row * stride
            line_a, line_b = pa.samples[start:start + stride], pb.samples[start:start + stride]
            total += stride
            if line_a != line_b:
                differing += sum(1 for x, y in zip(line_a, line_b) if abs(x - y) > 24)
        worst = max(worst, differing / total if total else 0.0)
    record("unchanged_elsewhere", worst <= MAX_PIXEL_DIFF_OUTSIDE,
           "nothing outside the reordered blocks changed" if worst <= MAX_PIXEL_DIFF_OUTSIDE
           else f"{worst:.3%} of pixels outside the reordered blocks changed")
    src.close()
    out.close()

    return {"passed": all(check["passed"] for check in checks.values()), "checks": checks}


# ==============================================================================
# ENTRY POINT
# ==============================================================================

@dataclass
class FaithfulResult:
    pdf_path: Path
    tailored: bool
    validation: Dict[str, object]
    changes: List[str]
    notes: List[str]


def tailor_pdf(source_pdf: Path, job: JobPosting, output: Path) -> FaithfulResult:
    """Produce a validated, reordered copy of the candidate's own resume for one job."""
    layout = read_layout(source_pdf)
    plan = plan_for_job(layout, job)
    notes: List[str] = []

    if not plan.moving:
        shutil.copyfile(source_pdf, output)
        validation = validate(layout, plan, output)
        notes.append("Already in the best order for this job; the original is used as is.")
        return FaithfulResult(output, False, validation, [], notes)

    notes.extend(build(layout, plan, output))
    validation = validate(layout, plan, output)
    if validation["passed"]:
        return FaithfulResult(output, True, validation, plan.changes(), notes)

    failed = [name for name, check in validation["checks"].items() if not check["passed"]]
    shutil.copyfile(source_pdf, output)
    fallback = validate(layout, Plan(groups=[], skipped=[]), output)
    notes.append(f"Reordering failed validation ({', '.join(failed)}); the original resume is used unchanged.")
    fallback["rejected_checks"] = validation["checks"]
    return FaithfulResult(output, False, fallback, [], notes)


def summarize(result: FaithfulResult) -> str:
    """One line for a spreadsheet cell."""
    checks = result.validation.get("checks", {})
    passed = sum(1 for check in checks.values() if check["passed"])
    status = "PASS" if result.validation.get("passed") else "FAIL"
    what = f"{len(result.changes)} section(s) reordered" if result.tailored else "original order kept"
    return f"{status} {passed}/{len(checks)} checks - {what}"
