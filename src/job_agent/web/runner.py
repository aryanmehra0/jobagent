"""Background phase execution with a live event stream.

Runs pipeline phases in a worker thread and publishes structured events
(`phase_start`, `log`, `phase_end`, `run_end`) to every connected dashboard over
Server-Sent Events.

Two deliberate constraints:

* **One run at a time.** Phase output is captured by redirecting `sys.stdout`,
  which is process-wide, and the phases share artifact files on disk. Concurrent
  runs would interleave both.
* **Cancellation happens between phases.** A phase in flight is left to finish
  rather than killed part-way, because several of them write files (and one of
  them submits job applications) that must not be torn in half.
"""

from __future__ import annotations

import contextlib
import io
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from job_agent.config.settings import settings
from job_agent.web.state import PHASE_ORDER, build_snapshot

# Events replayed to a dashboard that connects mid-run, so a late arrival still
# sees the log rather than an empty panel.
MAX_REPLAY_EVENTS = 600


class _LineTee(io.TextIOBase):
    """Splits written text into lines and forwards each to a callback.

    Also writes through to the original stream, so the terminal that launched the
    server still shows the same output the dashboard is displaying.
    """

    def __init__(self, sink: Callable[[str], None], passthrough: Optional[io.TextIOBase] = None):
        self._sink = sink
        self._passthrough = passthrough
        self._buffer = ""

    def write(self, text: str) -> int:
        if self._passthrough is not None:
            try:
                self._passthrough.write(text)
            except Exception:
                pass
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._sink(line.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buffer.strip():
            self._sink(self._buffer.rstrip())
            self._buffer = ""
        if self._passthrough is not None:
            try:
                self._passthrough.flush()
            except Exception:
                pass

    def isatty(self) -> bool:
        # Reported as non-interactive so rich renders plain, fixed-width output
        # instead of emitting cursor-control escape sequences into the log.
        return False


class RunCancelled(Exception):
    """Raised between phases when the user has asked the run to stop."""


class PipelineRunner:
    """Executes phases in the background and broadcasts their progress."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List["queue.Queue[Dict[str, Any]]"] = []
        self._subscriber_lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._cancel = threading.Event()
        self._replay: List[Dict[str, Any]] = []
        self.current: Optional[Dict[str, Any]] = None

    # --- Event plumbing -------------------------------------------------------

    def subscribe(self) -> "queue.Queue[Dict[str, Any]]":
        """Register a dashboard connection and prime it with recent history."""
        channel: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=2000)
        with self._subscriber_lock:
            for event in self._replay:
                with contextlib.suppress(queue.Full):
                    channel.put_nowait(event)
            self._subscribers.append(channel)
        return channel

    def unsubscribe(self, channel: "queue.Queue[Dict[str, Any]]") -> None:
        """Drop a dashboard connection."""
        with self._subscriber_lock:
            if channel in self._subscribers:
                self._subscribers.remove(channel)

    def publish(self, event: Dict[str, Any]) -> None:
        """Broadcast one event to every connected dashboard."""
        event.setdefault("ts", time.time())
        with self._subscriber_lock:
            self._replay.append(event)
            if len(self._replay) > MAX_REPLAY_EVENTS:
                del self._replay[: len(self._replay) - MAX_REPLAY_EVENTS]
            for channel in list(self._subscribers):
                try:
                    channel.put_nowait(event)
                except queue.Full:
                    # A dashboard that stopped reading must not stall the run.
                    self._subscribers.remove(channel)

    # --- Run control ----------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Whether a run is currently in flight."""
        return self._thread is not None and self._thread.is_alive()

    def cancel(self) -> bool:
        """Ask the current run to stop after the phase in flight completes."""
        if not self.is_running:
            return False
        self._cancel.set()
        self.publish({"type": "log", "phase": None, "line": "Cancellation requested; "
                                                            "finishing the current phase first."})
        return True

    def start(self, phases: List[str], options: Dict[str, Any]) -> Optional[str]:
        """Begin a run. Returns an error message, or None if it started.

        Args:
            phases: Phase names to run, in order.
            options: Per-run settings (`resume`, `dry_run`, `limit`, `threshold`,
                `track_all`, `no_ats`).
        """
        unknown = [name for name in phases if name not in PHASE_ORDER]
        if unknown:
            return f"Unknown phase(s): {', '.join(unknown)}"
        if not phases:
            return "No phases selected."

        with self._lock:
            if self.is_running:
                return "A run is already in progress."
            self._cancel.clear()
            self._replay.clear()
            self.current = {
                "phases": phases,
                "options": options,
                "started_at": time.time(),
                "results": {},
            }
            self._thread = threading.Thread(
                target=self._run, args=(phases, options), name="pipeline-run", daemon=True
            )
            self._thread.start()
        return None

    # --- Execution ------------------------------------------------------------

    def _run(self, phases: List[str], options: Dict[str, Any]) -> None:
        """Worker body: execute each phase in turn, streaming its output."""
        self.publish({"type": "run_start", "phases": phases, "options": _safe_options(options)})
        overall = "ok"

        def emit_log(phase: Optional[str]) -> Callable[[str], None]:
            return lambda line: self.publish({"type": "log", "phase": phase, "line": line})

        for name in phases:
            if self._cancel.is_set():
                self.publish({"type": "phase_end", "phase": name, "status": "cancelled",
                              "duration": 0.0, "summary": {}, "error": None})
                overall = "cancelled"
                break

            self.publish({"type": "phase_start", "phase": name})
            started = time.perf_counter()
            tee = _LineTee(emit_log(name), passthrough=_original_stdout())

            try:
                with contextlib.redirect_stdout(tee), contextlib.redirect_stderr(tee):
                    summary = _PHASE_IMPLS[name](options, self._cancel)
                    tee.flush()
                self.publish({
                    "type": "phase_end",
                    "phase": name,
                    "status": summary.pop("_status", "ok"),
                    "duration": round(time.perf_counter() - started, 2),
                    "summary": summary,
                    "error": None,
                })
                if self.current is not None:
                    self.current["results"][name] = summary

                # A phase that produced nothing makes the phases after it
                # meaningless, so the run stops here with a clear reason.
                halt = summary.get("halt_reason")
                if halt:
                    self.publish({"type": "log", "phase": name, "line": f"Stopping: {halt}"})
                    overall = "halted"
                    break

            except Exception as exc:
                tee.flush()
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                self.publish({
                    "type": "phase_end",
                    "phase": name,
                    "status": "error",
                    "duration": round(time.perf_counter() - started, 2),
                    "summary": {},
                    "error": detail,
                })
                overall = "error"
                break

        self.publish({"type": "state", "snapshot": build_snapshot()})
        self.publish({"type": "run_end", "status": overall})


def _original_stdout():
    """The real stdout, bypassing any active redirect."""
    import sys

    return getattr(sys, "__stdout__", None)


def _safe_options(options: Dict[str, Any]) -> Dict[str, Any]:
    """Options shaped for display, with paths reduced to file names."""
    shown = dict(options)
    if shown.get("resume"):
        shown["resume"] = Path(shown["resume"]).name
    return shown


# ==============================================================================
# PHASE IMPLEMENTATIONS
#
# Each returns a summary dict for the dashboard. `halt_reason` stops the run
# cleanly; `_status` overrides the reported status (used for "skipped").
# ==============================================================================

def _phase_intake(options: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
    """Parse the selected resume into a sealed profile."""
    from job_agent.intake.parser import ResumeParser
    from job_agent.intake.validator import describe_profile_gaps

    resume = options.get("resume")
    if resume:
        pdf_path = Path(resume)
        if not pdf_path.is_absolute():
            pdf_path = settings.raw_resumes_dir / pdf_path
    else:
        candidates = sorted(settings.raw_resumes_dir.glob("*.pdf"))
        if not candidates:
            raise FileNotFoundError(
                "No resume PDF available. Upload one from the dashboard first."
            )
        pdf_path = candidates[0]

    if not pdf_path.exists():
        raise FileNotFoundError(f"Resume not found: {pdf_path}")

    profile = ResumeParser().parse(pdf_path=pdf_path, output_path=settings.profile_path)
    gaps = describe_profile_gaps(profile)

    return {
        "candidate": profile.contact.full_name,
        "roles": len(profile.experience),
        "years": profile.years_of_experience,
        "locked_facts": len(profile.all_locked_facts()),
        "extracted_by": profile.extraction_method,
        "gaps": gaps,
    }


def _phase_source(options: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
    """Sweep the job boards and ATS feeds."""
    from job_agent.intake.cli import load_search_parameters
    from job_agent.sourcing.scraper import OmnichannelScraper

    params = load_search_parameters(settings.searches_path)
    scraper = OmnichannelScraper(search_params=params)
    jobs = scraper.run_sourcing_pipeline(include_ats_direct=not options.get("no_ats", False))

    summary: Dict[str, Any] = {
        "found": len(jobs),
        "filtered": dict(scraper.filter_stats),
        "domains": params.target_domains,
    }
    if not jobs:
        summary["halt_reason"] = (
            "no new jobs were found. Widen the search in Settings, or clear the "
            "delta store so previously seen jobs are considered again."
        )
    return summary


def _phase_evaluate(options: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
    """Score sourced jobs against the profile."""
    from job_agent.evaluation.pipeline import SemanticEvaluationPipeline

    evaluated, qualified = SemanticEvaluationPipeline().run_evaluation(
        tier1_threshold=options.get("threshold"),
        limit=options.get("limit"),
    )

    summary: Dict[str, Any] = {"scored": len(evaluated), "qualified": len(qualified)}
    if not qualified:
        summary["halt_reason"] = (
            f"nothing reached the fit threshold of {settings.min_match_score:g}/10. "
            "Lower MIN_MATCH_SCORE, or widen the Tier 1 threshold."
        )
    return summary


def _phase_tailor(options: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
    """Compile a bespoke resume for each qualified job."""
    from job_agent.tailoring.pipeline import ResumeTailoringPipeline

    records = ResumeTailoringPipeline().run_tailoring(limit=options.get("limit"))

    summary: Dict[str, Any] = {
        "compiled": len(records),
        "restored": sum(len(item.get("restored_metrics", [])) for item in records),
        "blocked": sum(len(item.get("dropped_fabrications", [])) for item in records),
    }
    if not records:
        summary["halt_reason"] = "no resumes were compiled."
    return summary


def _phase_apply(options: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
    """Submit (or simulate submitting) the tailored applications."""
    from job_agent.automation.pipeline import AutoApplyPipeline

    dry_run = bool(options.get("dry_run", True))
    successful, failed = AutoApplyPipeline().run_applications(
        dry_run=dry_run,
        limit=options.get("limit"),
        # The dashboard obtains consent in the browser before a live run, so the
        # terminal prompt would deadlock with nobody at the keyboard.
        assume_yes=True,
    )
    dry_runs = sum(1 for item in successful if item.get("status") == "dry_run")
    return {
        "dry_run": dry_run,
        "submitted": len(successful) - dry_runs,
        "simulated": dry_runs,
        "fallbacks": len(failed),
    }


def _phase_track(options: Dict[str, Any], cancel: threading.Event) -> Dict[str, Any]:
    """Write the tracking workbook and outreach emails."""
    from job_agent.tracking.pipeline import FallbackTrackingPipeline

    records = FallbackTrackingPipeline().process_fallbacks(
        force_track_all=bool(options.get("track_all", True))
    )
    return {"logged": len(records), "tracker": str(settings.tracker_path)}


_PHASE_IMPLS: Dict[str, Callable[[Dict[str, Any], threading.Event], Dict[str, Any]]] = {
    "intake": _phase_intake,
    "source": _phase_source,
    "evaluate": _phase_evaluate,
    "tailor": _phase_tailor,
    "apply": _phase_apply,
    "track": _phase_track,
}
