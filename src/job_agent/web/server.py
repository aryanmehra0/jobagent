"""Local HTTP server for the flow console.

Built on the standard library only, so the dashboard runs with no extra
dependencies beyond what the pipeline already needs.

Security posture. The server can trigger real job applications, so it is not a
neutral read-only endpoint:

* It binds to loopback only; it is never reachable from the network.
* Every mutating request must carry a per-session token, which is generated at
  startup and embedded in the served page. A malicious site the user happens to
  visit can issue a cross-origin POST to localhost but cannot read the response
  that contains the token, so it cannot forge one.
* `Host` and `Origin` are checked against the bound address, which closes the
  DNS-rebinding path that would otherwise defeat the loopback bind.
* Live (non-dry-run) applying requires an explicit confirmation field in the
  request body, so it can never be the result of a stray click.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import queue
import secrets
import socket
import threading
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import SecretStr

import job_agent.config.settings as settings_module
from job_agent.config.settings import settings
from job_agent.web.runner import PipelineRunner
from job_agent.web.state import build_snapshot

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Uploaded resumes are capped well above any real CV; the limit exists to stop a
# runaway request from filling memory.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_BODY_BYTES = 1 * 1024 * 1024

# How long a parked SSE connection waits before emitting a keep-alive comment.
SSE_HEARTBEAT_SECONDS = 20.0


class FlowConsoleServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the shared runner and session token."""

    daemon_threads = True
    # SO_REUSEADDR means different things on the two platforms. On POSIX it only
    # permits rebinding a port left in TIME_WAIT, which is what we want after a
    # restart. On Windows it permits two live sockets to share a port, so a second
    # `job-agent ui` on the same port would bind silently and then split incoming
    # requests between two consoles at random. Windows gets exclusive binding
    # instead, so the second start fails loudly with "address already in use".
    allow_reuse_address = os.name != "nt"

    def __init__(self, address: Tuple[str, int], handler_class, token: str):
        super().__init__(address, handler_class)
        self.token = token
        self.runner = PipelineRunner()

    def server_bind(self) -> None:
        """Bind the listening socket, claiming the port exclusively on Windows."""
        if os.name == "nt":
            with contextlib.suppress(OSError, AttributeError):
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class FlowConsoleHandler(BaseHTTPRequestHandler):
    """Routes dashboard requests."""

    server_version = "JobAgentFlowConsole/1.0"
    protocol_version = "HTTP/1.1"

    # --- Plumbing -------------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence per-request logging; the pipeline's own output is the signal."""

    def _send(
        self,
        status: HTTPStatus,
        body: bytes,
        content_type: str = "application/json",
        extra_headers: Optional[Dict[str, str]] = None,
    ) -> None:
        """Write a complete response."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The console is single-origin; no other page should be able to frame or
        # script it.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        """Send a JSON response."""
        self._send(status, json.dumps(payload, default=str).encode("utf-8"))

    def _error(self, status: HTTPStatus, message: str) -> None:
        """Send a JSON error the dashboard can display verbatim."""
        self._json(status, {"error": message})

    def _read_body(self) -> bytes:
        """Read the request body, refusing anything oversized."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return b""
        limit = MAX_UPLOAD_BYTES if self.path.startswith("/api/resume") else MAX_BODY_BYTES
        if length > limit:
            raise ValueError(f"Request body exceeds the {limit // (1024 * 1024)} MB limit.")
        return self.rfile.read(length) if length else b""

    def _check_origin(self) -> bool:
        """Reject requests whose Host or Origin is not this loopback server.

        Without this, a page on the open internet could resolve its own hostname
        to 127.0.0.1 and reach the console despite the loopback bind.
        """
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "[::1]", "::1"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            hostname = urlparse(origin).hostname
            if hostname not in ("127.0.0.1", "localhost", "::1"):
                return False
        return True

    def _check_token(self) -> bool:
        """Whether the request carries this session's token."""
        supplied = self.headers.get("X-Session-Token") or ""
        return secrets.compare_digest(supplied, self.server.token)

    # --- Routing --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        """Serve the dashboard, its assets, and read-only API endpoints."""
        if not self._check_origin():
            self._error(HTTPStatus.FORBIDDEN, "Requests are accepted from localhost only.")
            return

        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._serve_index()
        elif route.startswith("/static/"):
            self._serve_static(route[len("/static/"):])
        elif route == "/api/state":
            from job_agent.runtime import pipeline_busy
            self._json(HTTPStatus.OK, {"state": build_snapshot(), "running": self.server.runner.is_running or pipeline_busy()})
        elif route == "/api/events":
            self._serve_events()
        elif route == "/api/analytics":
            from job_agent.web.analytics import analytics
            self._json(HTTPStatus.OK, analytics())
        elif route == "/api/jobs":
            from job_agent.tracking.export import JobsCsvExporter, _read_json
            from job_agent.web.state import run_report_state
            from job_agent.runtime import pipeline_lock
            exporter = JobsCsvExporter()
            warnings = []
            try:
                # Refresh derived dates/readiness without running a new job search.
                with pipeline_lock():
                    exporter.export()
            except (RuntimeError, OSError) as exc:
                warnings.append(f"Showing the saved export; refresh could not finish: {exc}")
            self._json(HTTPStatus.OK, {"jobs": list(exporter.load().values()), "warnings": warnings,
                       "csv_path": str(settings.outputs_dir / "jobs_master.csv"),
                       "latest_csv_path": str(settings.outputs_dir / "jobs_latest.csv"),
                       "ready_csv_path": str(settings.outputs_dir / "applications_ready.csv"),
                       "outreach_dir": str(settings.outputs_dir / "outreach"),
                       "manually_applied": list(_read_json(settings.outputs_dir / "manual_applications.json", {})),
                       "run_report": run_report_state(),
                       "coverage": _read_json(settings.outputs_dir / "source_coverage.json", {})})
        elif route == "/api/file":
            query = parse_qs(parsed.query)
            self._serve_artifact(query.get("path", [""])[0], download=query.get("download") == ["1"])
        else:
            self._error(HTTPStatus.NOT_FOUND, f"No route for {route}")

    def do_POST(self) -> None:  # noqa: N802
        """Handle the mutating endpoints, all of which require the session token."""
        if not self._check_origin():
            self._error(HTTPStatus.FORBIDDEN, "Requests are accepted from localhost only.")
            return
        if not self._check_token():
            self._error(HTTPStatus.UNAUTHORIZED, "Missing or invalid session token. Reload the page.")
            return

        route = urlparse(self.path).path
        try:
            body = self._read_body()
        except ValueError as exc:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(exc))
            return

        handlers = {
            "/api/resume": self._upload_resume,
            "/api/resume/delete": self._delete_resume,
            "/api/resume/check": self._check_resume,
            "/api/config": self._save_config,
            "/api/llm": self._save_llm,
            "/api/preferences": self._save_preferences,
            "/api/outputs/archive": self._archive_outputs,
            "/api/run": self._start_run,
            "/api/cancel": self._cancel_run,
            "/api/export/bundle": self._export_bundle,
            "/api/jobs/applied": self._mark_applied,
        }
        handler = handlers.get(route)
        if handler is None:
            self._error(HTTPStatus.NOT_FOUND, f"No route for {route}")
            return

        try:
            handler(body)
        except Exception as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"{type(exc).__name__}: {exc}")

    # --- Static assets --------------------------------------------------------

    def _serve_index(self) -> None:
        """Serve the dashboard with this session's token injected."""
        index = STATIC_DIR / "index.html"
        if not index.exists():
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "Dashboard assets are missing.")
            return
        html = index.read_text(encoding="utf-8").replace("__SESSION_TOKEN__", self.server.token)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8")

    def _serve_static(self, relative: str) -> None:
        """Serve a file from the static directory, refusing path traversal."""
        target = (STATIC_DIR / unquote(relative)).resolve()
        if not target.is_file() or STATIC_DIR.resolve() not in target.parents:
            self._error(HTTPStatus.NOT_FOUND, "Asset not found.")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type)

    def _serve_artifact(self, raw_path: str, download: bool = False) -> None:
        """Serve a generated artifact (a tailored PDF, the workbook, a JSON file).

        Restricted to the project's own data directory so the console cannot be
        used to read arbitrary files off the machine.
        """
        if not raw_path:
            self._error(HTTPStatus.BAD_REQUEST, "No path supplied.")
            return
        target = Path(unquote(raw_path)).resolve()
        allowed_root = settings.data_dir.resolve()
        if allowed_root not in target.parents or not target.is_file():
            self._error(HTTPStatus.FORBIDDEN, "That file is outside the agent's data directory.")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix.lower() == ".csv":
            content_type = "text/csv; charset=utf-8"
        # Spreadsheets are for opening in Excel, not rendering as text in a tab.
        disposition = "attachment" if download or target.suffix.lower() in (".csv", ".xlsx", ".zip", ".eml") else "inline"
        self._send(
            HTTPStatus.OK,
            target.read_bytes(),
            content_type,
            {"Content-Disposition": f'{disposition}; filename="{target.name}"'},
        )

    def _export_bundle(self, body: bytes) -> None:
        from job_agent.tracking.bundle import build_application_pack
        if self.server.runner.is_running:
            self._error(HTTPStatus.CONFLICT, "Wait for the current run to finish before downloading a pack.")
            return
        try:
            path = build_application_pack()
        except RuntimeError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return
        self._json(HTTPStatus.OK, {"path": str(path)})

    def _mark_applied(self, body: bytes) -> None:
        if self.server.runner.is_running:
            self._error(HTTPStatus.CONFLICT, "Wait for the run to finish before updating applications.")
            return
        from job_agent.tracking.manual import mark_applied
        try:
            payload = json.loads(body or b"{}")
            if not isinstance(payload, dict) or not isinstance(payload.get("job_id"), str) or not isinstance(payload.get("undo", False), bool):
                raise ValueError("Supply a job ID and an optional boolean undo flag.")
            result = mark_applied(payload["job_id"], undo=payload.get("undo", False))
        except ValueError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        except RuntimeError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
            return
        self._json(HTTPStatus.OK, result)

    # --- Server-sent events ---------------------------------------------------

    def _serve_events(self) -> None:
        """Stream run events until the dashboard disconnects."""
        channel = self.server.runner.subscribe()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

        try:
            while True:
                try:
                    event = channel.get(timeout=SSE_HEARTBEAT_SECONDS)
                    payload = f"data: {json.dumps(event, default=str)}\n\n"
                except queue.Empty:
                    # A comment frame keeps proxies and the browser from closing
                    # an idle connection.
                    payload = ": keep-alive\n\n"
                self.wfile.write(payload.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, OSError):
            pass
        finally:
            self.server.runner.unsubscribe(channel)

    # --- Mutating endpoints ---------------------------------------------------

    def _upload_resume(self, body: bytes) -> None:
        """Store an uploaded PDF in the resumes directory.

        The browser posts raw bytes with the name in a header, which avoids
        multipart parsing (the stdlib `cgi` module was removed in Python 3.13).
        """
        from job_agent.intake.parser import SUPPORTED_RESUME_SUFFIXES

        raw_name = self.headers.get("X-Filename") or "resume.pdf"
        # Reduce to a bare filename so an upload cannot escape the directory.
        safe_name = Path(unquote(raw_name)).name
        suffix = Path(safe_name).suffix.lower()

        if suffix not in SUPPORTED_RESUME_SUFFIXES:
            self._error(
                HTTPStatus.BAD_REQUEST,
                f"Unsupported file type. Accepted: {', '.join(SUPPORTED_RESUME_SUFFIXES)}.",
            )
            return
        if not body:
            self._error(HTTPStatus.BAD_REQUEST, "The uploaded file was empty.")
            return

        # Check the content matches the extension, so a renamed file is rejected
        # here rather than failing confusingly during extraction.
        if suffix == ".pdf" and not body.startswith(b"%PDF"):
            self._error(HTTPStatus.BAD_REQUEST, "That file is not a PDF.")
            return
        if suffix == ".docx" and not body.startswith(b"PK"):
            self._error(
                HTTPStatus.BAD_REQUEST,
                "That file is not a .docx. If it is an old .doc, open it and save as .docx.",
            )
            return

        settings.raw_resumes_dir.mkdir(parents=True, exist_ok=True)
        target = settings.raw_resumes_dir / safe_name
        target.write_bytes(body)
        self._json(HTTPStatus.OK, {"ok": True, "name": safe_name, "state": build_snapshot()})

    def _delete_resume(self, body: bytes) -> None:
        """Delete one resume PDF from the resumes directory.

        Scoped tightly: the name is reduced to a bare filename, must end in .pdf,
        and the resolved path must sit directly inside the resumes directory. The
        console is not a general-purpose file manager, and this endpoint exists
        only so the bundled demo resume can be cleared out without leaving the UI.
        """
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")
            return

        name = Path(unquote(str(payload.get("name") or ""))).name
        if not name.lower().endswith(".pdf"):
            self._error(HTTPStatus.BAD_REQUEST, "Only PDF resumes can be removed.")
            return

        target = (settings.raw_resumes_dir / name).resolve()
        if target.parent != settings.raw_resumes_dir.resolve() or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, f"No resume named {name}.")
            return

        target.unlink()
        self._json(HTTPStatus.OK, {"ok": True, "removed": name, "state": build_snapshot()})

    def _check_resume(self, body: bytes) -> None:
        """Diagnose a resume without importing it.

        Lets the setup wizard show the same readiness report the `check` command
        prints, so a candidate learns their template confuses the parser before
        they build a profile from it.
        """
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")
            return

        name = Path(unquote(str(payload.get("name") or ""))).name
        target = (settings.raw_resumes_dir / name).resolve()
        if target.parent != settings.raw_resumes_dir.resolve() or not target.is_file():
            self._error(HTTPStatus.NOT_FOUND, f"No resume named {name}.")
            return

        from job_agent.intake.readiness import check_resume

        self._json(HTTPStatus.OK, {"ok": True, "report": check_resume(target).to_dict()})

    def _save_config(self, body: bytes) -> None:
        """Validate and persist search parameters."""
        from job_agent.config.schema import SearchParameters
        from job_agent.intake.cli import save_search_parameters

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")
            return

        try:
            params = SearchParameters(**payload)
        except Exception as exc:
            # Surfaced verbatim: the schema's messages name the offending field.
            self._error(HTTPStatus.BAD_REQUEST, _format_validation_error(exc))
            return

        save_search_parameters(params, settings.searches_path)
        self._json(HTTPStatus.OK, {"ok": True, "state": build_snapshot()})

    def _save_llm(self, body: bytes) -> None:
        """Persist local LLM provider settings to .env and update this process."""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")
            return
        if self.server.runner.is_running:
            self._error(HTTPStatus.CONFLICT, "Wait for the current run to finish before changing LLM settings.")
            return

        try:
            provider = _save_llm_settings(payload)
        except Exception as exc:
            self._error(HTTPStatus.BAD_REQUEST, _format_validation_error(exc))
            return
        self._json(HTTPStatus.OK, {"ok": True, "provider": provider, "state": build_snapshot()})

    def _save_preferences(self, body: bytes) -> None:
        """Store country, sponsorship and salary preferences and apply them to the profile."""
        from job_agent.intake.preferences import save_preferences

        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")
            return
        if self.server.runner.is_running:
            self._error(HTTPStatus.CONFLICT, "Wait for the current run to finish before changing preferences.")
            return
        if not settings.profile_path.is_file():
            self._error(HTTPStatus.BAD_REQUEST, "Build your profile from a resume first.")
            return
        try:
            save_preferences(payload)
        except Exception as exc:
            self._error(HTTPStatus.BAD_REQUEST, _format_validation_error(exc))
            return
        self._json(HTTPStatus.OK, {"ok": True, "state": build_snapshot()})

    def _archive_outputs(self, body: bytes) -> None:
        """Move the last run's results to history so the flow starts clean.

        The seen-jobs store, outreach ledger, jobs CSV and tracker are kept:
        they are what stops a new run repeating jobs or emails.
        """
        from job_agent.runtime import invalidate_after

        if self.server.runner.is_running:
            self._error(HTTPStatus.CONFLICT, "Stop the current run before starting fresh.")
            return
        invalidate_after("intake", settings.outputs_dir)
        self._json(HTTPStatus.OK, {"ok": True, "state": build_snapshot()})

    def _start_run(self, body: bytes) -> None:
        """Begin a run of the requested phases."""
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError as exc:
            self._error(HTTPStatus.BAD_REQUEST, f"Invalid JSON: {exc}")
            return

        phases = payload.get("phases") or []
        options = payload.get("options") or {}

        # Live applying is gated on an explicit field, so no single click can
        # send real applications.
        if "apply" in phases and not options.get("dry_run", True):
            if payload.get("confirm_live") != "APPLY":
                self._error(
                    HTTPStatus.BAD_REQUEST,
                    "Live applying requires explicit confirmation.",
                )
                return

        error = self.server.runner.start(phases, options)
        if error:
            self._error(HTTPStatus.CONFLICT, error)
            return
        self._json(HTTPStatus.ACCEPTED, {"ok": True, "phases": phases})

    def _cancel_run(self, body: bytes) -> None:
        """Ask the current run to stop after the running phase finishes."""
        cancelled = self.server.runner.cancel()
        self._json(HTTPStatus.OK, {"ok": cancelled, "running": self.server.runner.is_running})


def _format_validation_error(exc: Exception) -> str:
    """Render a pydantic validation error as one readable line."""
    errors = getattr(exc, "errors", None)
    if not callable(errors):
        return str(exc)
    try:
        return "; ".join(
            f"{'.'.join(str(part) for part in item['loc']) or 'input'}: {item['msg']}"
            for item in errors()
        )
    except Exception:
        return str(exc)


def _save_llm_settings(payload: Dict[str, Any]) -> str:
    """Write LLM settings to .env.

    Blank key fields mean "keep the existing secret" so a user can switch models
    without retyping an API key.
    """
    provider = str(payload.get("provider") or "").strip().lower()
    valid = {"openai", "anthropic", "groq", "openai_compatible", "none"}
    if provider not in valid:
        raise ValueError(f"Provider must be one of {', '.join(sorted(valid))}.")

    def text(name: str) -> str:
        return str(payload.get(name) or "").strip()

    updates: Dict[str, str] = {"DEFAULT_LLM_PROVIDER": provider}
    if payload.get("llm_strict") is not None:
        updates["LLM_STRICT"] = "true" if bool(payload.get("llm_strict")) else "false"

    if provider == "groq":
        key = text("groq_api_keys")
        if key:
            updates["GROQ_API_KEYS"] = key
        if text("groq_model"):
            updates["GROQ_MODEL"] = text("groq_model")
        if text("groq_fallback_model"):
            updates["GROQ_FALLBACK_MODEL"] = text("groq_fallback_model")
        if not key and not settings.groq_keys:
            raise ValueError("Add at least one Groq key, or choose a deterministic fallback.")
    elif provider == "openai":
        key = text("openai_api_key")
        if key:
            updates["OPENAI_API_KEY"] = key
        if text("openai_model"):
            updates["LLM_INTAKE_MODEL"] = text("openai_model")
            updates["LLM_RERANK_MODEL"] = text("openai_model")
            updates["LLM_TAILOR_MODEL"] = text("openai_model")
        if not key and not settings.openai_api_key:
            raise ValueError("Add an OpenAI key, or choose a deterministic fallback.")
    elif provider == "anthropic":
        key = text("anthropic_api_key")
        if key:
            updates["ANTHROPIC_API_KEY"] = key
        if text("anthropic_model"):
            updates["ANTHROPIC_MODEL"] = text("anthropic_model")
        if not key and not settings.anthropic_api_key:
            raise ValueError("Add an Anthropic key, or choose a deterministic fallback.")
    elif provider == "openai_compatible":
        key = text("openai_compatible_api_key")
        base_url = text("openai_compatible_base_url")
        model = text("openai_compatible_model")
        if key:
            updates["OPENAI_COMPATIBLE_API_KEY"] = key
        if base_url:
            updates["OPENAI_COMPATIBLE_BASE_URL"] = base_url.rstrip("/")
        if model:
            updates["OPENAI_COMPATIBLE_MODEL"] = model
        if not (key or settings.openai_compatible_api_key):
            raise ValueError("Add an OpenAI-compatible API key.")
        if not (base_url or settings.openai_compatible_base_url):
            raise ValueError("Add the OpenAI-compatible base URL.")
        if not (model or settings.openai_compatible_model):
            raise ValueError("Add the OpenAI-compatible model name.")

    _merge_env(settings_module.dotenv_path, updates)
    _apply_llm_updates(updates)
    return settings.active_provider


def _merge_env(path: Path, updates: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(updates)
    out = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            out.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            out.append(f"{key}={_env_value(remaining.pop(key))}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={_env_value(value)}")
    temporary = path.with_suffix(".env.tmp")
    temporary.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    temporary.replace(path)


def _env_value(value: str) -> str:
    if any(ch.isspace() for ch in value) or "#" in value:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _apply_llm_updates(updates: Dict[str, str]) -> None:
    for key, value in updates.items():
        os.environ[key] = value
    mapping = {
        "DEFAULT_LLM_PROVIDER": ("default_llm_provider", str),
        "OPENAI_API_KEY": ("openai_api_key", str),
        "ANTHROPIC_API_KEY": ("anthropic_api_key", str),
        "GROQ_MODEL": ("groq_model", str),
        "GROQ_FALLBACK_MODEL": ("groq_fallback_model", str),
        "LLM_INTAKE_MODEL": ("llm_intake_model", str),
        "LLM_RERANK_MODEL": ("llm_rerank_model", str),
        "LLM_TAILOR_MODEL": ("llm_tailor_model", str),
        "ANTHROPIC_MODEL": ("anthropic_model", str),
        "OPENAI_COMPATIBLE_API_KEY": ("openai_compatible_api_key", str),
        "OPENAI_COMPATIBLE_BASE_URL": ("openai_compatible_base_url", str),
        "OPENAI_COMPATIBLE_MODEL": ("openai_compatible_model", str),
        "LLM_STRICT": ("llm_strict", lambda v: v.lower() == "true"),
    }
    for key, value in updates.items():
        if key == "GROQ_API_KEYS":
            settings.groq_api_keys = SecretStr(value)
            continue
        if key in mapping:
            attr, caster = mapping[key]
            setattr(settings, attr, caster(value))


def run_server(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    """Start the flow console and block until interrupted."""
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            "The flow console binds to loopback only: it can start browser sessions "
            "and submit job applications, so it must not be exposed on a network."
        )

    token = secrets.token_urlsafe(32)
    handler = partial(FlowConsoleHandler)
    server = FlowConsoleServer((host, port), handler, token)
    url = f"http://{host}:{port}/"

    print(f"Flow console running at {url}")
    print("Press Ctrl+C to stop.\n")

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down the flow console.")
    finally:
        server.shutdown()
        server.server_close()
