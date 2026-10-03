"""Guards for the dashboard's static front end.

The console is plain HTML + one JS file with no build step, so a typo'd element
id fails only in the browser: `$("#x")` returns null and the whole page stops
rendering. These checks catch that class of mistake without needing a browser.
"""
from __future__ import annotations

import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "job_agent" / "web" / "static"

# Elements app.js creates itself (setup wizard / settings form) before looking
# them up, so they are correctly absent from index.html.
CREATED_BY_JS = {"resume-pick", "find_contacts"}


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def test_every_static_element_lookup_exists_in_index_html() -> None:
    used = set(re.findall(r'\$\("#([A-Za-z][\w-]*)"\)', _read("app.js")))
    present = set(re.findall(r'\bid="([^"]+)"', _read("index.html")))
    missing = used - present - CREATED_BY_JS
    assert not missing, f"app.js looks up ids that index.html does not define: {sorted(missing)}"


def test_html_ids_are_unique() -> None:
    ids = re.findall(r'\bid="([^"]+)"', _read("index.html"))
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicates, f"duplicate ids in index.html: {duplicates}"


def test_inspector_tabs_are_wired_for_assistive_tech() -> None:
    html = _read("index.html")
    tabs = re.findall(r'<button class="tab[^"]*"[^>]*>', html)
    assert len(tabs) == 5
    for tab in tabs:
        assert 'role="tab"' in tab and "aria-selected=" in tab and "aria-controls=" in tab
        panel = re.search(r'aria-controls="([^"]+)"', tab).group(1)
        assert f'id="{panel}"' in html, f"tab controls missing panel {panel}"


def test_icon_only_buttons_have_accessible_names() -> None:
    html = _read("index.html")
    for button_id in ("theme-btn", "log-copy", "log-clear"):
        tag = re.search(rf'<button[^>]*id="{button_id}"[^>]*>', html).group(0)
        assert "aria-label=" in tag, f"#{button_id} has no accessible name"


def test_replayed_run_history_cannot_set_a_phase_status() -> None:
    """The server replays its last run on every connection. If that replay set a phase's
    status, a phase that failed this morning showed "Failed" for ever, even after it had
    succeeded elsewhere, because event status outranks the files on disk."""
    js = _read("app.js")
    for write in ('runStatus[evt.phase] = "running";', 'runStatus[evt.phase] = evt.status === "warning"'):
        before = js[: js.index(write)]
        guard = before.rfind("if (!replaying)")
        assert guard != -1 and "case \"" in before[guard:] or before[guard:].count("\n") < 4, \
            f"{write!r} must sit inside an `if (!replaying)` guard"
    assert "source.onopen" in js and "replaying = true" in js, "every reconnection replays history again"


def test_the_dashboard_follows_runs_started_outside_it() -> None:
    js = _read("app.js")
    assert "startStatePolling" in js and "syncState" in js


def test_source_panel_explains_dropped_listings():
    script = (STATIC / "app.js").read_text(encoding="utf-8") if "STATIC" in globals() else None
    if script is None:
        from pathlib import Path
        script = (Path(__file__).resolve().parents[1] / "src" / "job_agent" / "web" / "static" / "app.js").read_text(encoding="utf-8")
    assert '"/api/skipped' in script or "/api/skipped?" in script
    assert "appendSkippedJobs(panel)" in script
    # Every reason the scraper can record has a plain-language label.
    from job_agent.sourcing import scraper
    import re
    recorded = set(re.findall(r'_reject\(\s*"(\w+)"', __import__("inspect").getsource(scraper)))
    recorded |= {"work_mode", "not_remote"}
    for reason in recorded:
        assert f"{reason}:" in script, f"no label for skip reason {reason!r}"
