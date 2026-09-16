"""Local flow console: a visual, node-graph view of the six-phase pipeline.

Serves a single-page dashboard on localhost that shows each phase as a node, runs
phases on demand, and streams their progress live. It is a front end over the same
code the CLI calls, not a reimplementation, so the two can be used interchangeably.
"""

from job_agent.web.server import run_server

__all__ = ["run_server"]
