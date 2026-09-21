"""Serialize artifact writers across CLI processes and dashboard threads."""
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
import os
import threading
import time

from job_agent.config.settings import settings

_lock = threading.RLock()
_local = threading.local()


class RunCancelled(Exception):
    """The user asked the run to stop; raised at the next safe point."""


@contextmanager
def cancellation(event: threading.Event):
    """Make `event` the cancellation token for the current thread.

    The token is thread-local: the dashboard runs a pipeline in its own worker
    thread, and Stop there must never interrupt a CLI command or another thread.
    """
    previous = getattr(_local, "cancel_event", None)
    _local.cancel_event = event
    try:
        yield
    finally:
        _local.cancel_event = previous


def check_cancelled() -> None:
    """Raise `RunCancelled` if a Stop was requested for this thread's run.

    Called at the top of each unit of work in every long loop — before a board
    request, before scoring or tailoring a job, before starting an application.
    It is never called in the middle of one, so stopping cannot leave a file half
    written or an application submitted but unrecorded. Outside a cancellable run
    (the CLI) no token is installed and this does nothing.
    """
    event = getattr(_local, "cancel_event", None)
    if event is not None and event.is_set():
        raise RunCancelled("Stopped by user.")


@contextmanager
def pipeline_lock():
    if not _lock.acquire(blocking=False):
        raise RuntimeError("Another pipeline operation is running. Wait for it to finish.")
    handle = None
    acquired = False
    try:
        if getattr(_local, "depth", 0):
            _local.depth += 1
            try:
                yield
            finally:
                _local.depth -= 1
            return
        settings.outputs_dir.mkdir(parents=True, exist_ok=True)
        handle = (settings.outputs_dir / ".pipeline.lock").open("a+b")
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise RuntimeError("Another CLI or dashboard is writing pipeline data. Wait for it to finish.") from None
        _local.depth = 1
        try:
            yield
        finally:
            _local.depth = 0
    finally:
        if handle is not None:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        _lock.release()


def exclusive_run(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with pipeline_lock():
            return function(*args, **kwargs)
    return wrapped


def pipeline_busy() -> bool:
    """Detect CLI workers as well as dashboard threads without waiting on them."""
    if getattr(_local, "depth", 0):
        return True
    try:
        with pipeline_lock():
            return False
    except RuntimeError:
        return True


def invalidate_after(stage: str, output_dir: Path) -> None:
    """Archive downstream reports so a new upstream batch cannot reuse them."""
    artifacts = {
        "intake": ["scraped_jobs.json", "latest_jobs.json", "source_coverage.json", "evaluated_jobs.json", "qualified_jobs.json",
                   "tailored_resumes/manifest.json", "application_results.json", "evaluation_progress.json"],
        "source": ["evaluated_jobs.json", "qualified_jobs.json",
                   "tailored_resumes/manifest.json", "application_results.json", "evaluation_progress.json"],
        "evaluate": ["tailored_resumes/manifest.json", "application_results.json"],
        "tailor": ["application_results.json"],
    }
    archive = output_dir / "history" / f"{time.time_ns()}_{stage}"
    for relative in artifacts[stage]:
        path = output_dir / relative
        if path.is_file():
            target = archive / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            path.replace(target)
