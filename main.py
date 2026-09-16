"""Entrypoint for autonomous job search agent.

Delegates execution to job_agent.cli.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure UTF-8 console output on Windows before anything prints.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# Ensure src/ is on sys.path when running directly as a script.
src_dir = Path(__file__).resolve().parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from job_agent.cli import cli

if __name__ == "__main__":
    cli()
