"""A tailored resume must keep the original's vertical rhythm: no gap the original did not have."""
from __future__ import annotations

from pathlib import Path

import fitz

from job_agent.config.schema import JobPosting
from job_agent.tailoring import faithful

BLUE = (0.12, 0.22, 0.39)


def _resume(path: Path, extra_headroom: float, bullets) -> Path:
    """One role. `extra_headroom` is how much farther the first bullet sits below the
    role's header than the bullets sit from each other, which is how real resumes look."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    def text(x, y, value, size=10, bold=False, color=(0, 0, 0)):
        page.insert_text((x, y), value, fontname="hebo" if bold else "helv", fontsize=size, color=color)

    text(230, 40, "ASHA VERMA", size=17, bold=True, color=BLUE)
    text(34, 80, "EXPERIENCE", size=10.5, bold=True, color=BLUE)
    page.draw_line((34, 83), (560, 83), color=BLUE, width=0.8)
    text(34, 98, "Sensing Lab", bold=True)
    text(470, 98, "Jan 2024 - Present", bold=True)
    y = 112 + extra_headroom
    for first, second in bullets:
        page.insert_text((37, y), "•", fontname="tiro", fontsize=10)
        text(47, y, first)
        text(47, y + 12, second)
        y += 26
    text(34, y + 14, "SKILLS", size=10.5, bold=True, color=BLUE)
    doc.save(str(path))
    return path


def _bullet_tops(path: Path) -> list[float]:
    page = fitz.open(str(path))[0]
    tops = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            if "".join(span["text"] for span in line["spans"]).strip()[:1] in ("•", "·"):   # extracts as a middle dot
                tops.append(round(line["bbox"][1], 1))
    return sorted(tops)


def _gaps(tops: list[float]) -> list[float]:
    return [round(b - a, 1) for a, b in zip(tops, tops[1:])]


BULLETS = [
    ("Managed vendor invoices and budget reviews for the", "finance team each quarter."),
    ("Built retrieval augmented generation agents with LLM", "tooling that answered support tickets."),
    ("Trained transformer language models on scientific", "text for retrieval benchmarks."),
]
JOB = JobPosting(id="s1", title="NLP Research Engineer", company="Acme", source="lever",
                 job_url="https://acme.example/j/1",
                 description="Transformer language models, retrieval, LLM agents and scientific text.")


def test_a_reordered_list_keeps_the_gaps_the_original_had(tmp_path):
    # The first bullet sits 9pt farther from the header than the bullets sit from
    # each other. Moving it into the middle must not carry that extra space along.
    source = _resume(tmp_path / "src.pdf", extra_headroom=9, bullets=BULLETS)
    result = faithful.tailor_pdf(source, JOB, tmp_path / "out.pdf")
    assert result.tailored and result.validation["passed"], (result.notes, result.validation)

    original, tailored = _gaps(_bullet_tops(source)), _gaps(_bullet_tops(tmp_path / "out.pdf"))
    assert set(original) == {26.0}
    assert tailored == original, f"gaps changed from {original} to {tailored}"


def test_the_first_bullet_keeps_the_same_distance_from_its_header(tmp_path):
    source = _resume(tmp_path / "src.pdf", extra_headroom=9, bullets=BULLETS)
    faithful.tailor_pdf(source, JOB, tmp_path / "out.pdf")
    assert _bullet_tops(tmp_path / "out.pdf")[0] == _bullet_tops(source)[0]


def test_label_style_keyword_lines_are_not_moved_to_the_front(tmp_path):
    bullets = BULLETS[:2] + [("Tools: PyTorch, transformers, FAISS, LLM retrieval", "agents, scientific text benchmarks.")]
    source = _resume(tmp_path / "src.pdf", extra_headroom=4, bullets=bullets)
    layout = faithful.read_layout(source)
    plan = faithful.plan_for_job(layout, JOB)
    group = next(g for g in plan.groups if len(g.units) == 3)
    assert group.order[2] == 2, "the Tools line stays where the candidate put it"
    assert sorted(group.order) == [0, 1, 2]
