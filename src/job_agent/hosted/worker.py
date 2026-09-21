"""Hosted queue worker.

The worker claims queued runs and records terminal state. Full public SaaS
deployments should execute each run inside a per-user workspace/container before
calling the existing CLI pipeline. This reference worker defaults to validation
mode so a deployment can be smoke-tested without submitting applications.
"""

from __future__ import annotations

import os
import time

from job_agent.hosted.queue import HostedQueue


def run_once(queue: HostedQueue) -> bool:
    run = queue.claim_next()
    if run is None:
        return False
    try:
        # The reference worker proves queue semantics without sharing one local
        # browser profile across hosted users. A production executor should map
        # run.user_id to isolated storage and then call the CLI in that workspace.
        if os.environ.get("HOSTED_WORKER_EXECUTE") == "1":
            raise RuntimeError(
                "HOSTED_WORKER_EXECUTE is intentionally not implemented in the reference worker. "
                "Use a per-user container executor before enabling hosted live runs."
            )
        queue.finish(run.id, ok=True)
    except Exception as exc:
        queue.finish(run.id, ok=False, error=str(exc))
    return True


def run_forever(poll_seconds: float = 2.0) -> None:
    queue = HostedQueue()
    print("Hosted queue worker started.")
    while True:
        worked = run_once(queue)
        if not worked:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    run_forever(float(os.environ.get("HOSTED_WORKER_POLL_SECONDS", "2")))
