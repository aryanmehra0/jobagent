# Agent design: Perceive, Reason, Memory, Plan, Act, Observe, Guardrails

This maps the standard agentic-AI framework onto what this codebase actually
does, file by file, verified against the source on 2026-09-30 — not an
aspirational spec written before the code, and not marketing copy. Where a
pillar is genuinely partial, it says so. Development across sessions was
iterative, not planned against this framework up front; this document
describes the shape the result actually has, and doubles as the checklist for
keeping it that way as the system grows.

## The loop, phase by phase

| Pillar | What it means here | Where it lives |
| --- | --- | --- |
| **Perceive** | Reading the outside world: a resume, a job board, a web page's DOM, an inbox, an employer's site | `intake/parser.py`+`layout.py` (resume), `sourcing/scraper.py`+`ats_direct.py`+`public_feeds.py`+`details.py` (job market), `automation/navigator.py` (a live page's form fields), `tracking/inbox.py` (email replies), `contacts/warm.py` (employer team pages) |
| **Reason** | Turning perceived input into a judgment or a piece of text | `evaluation/reranker.py` (LLM fit judge, 1.0–10.0, with reasons), `tailoring/rewriter.py` (which bullets to lead with), `generation.py`'s `evidence_for` (which resume evidence answers an interview question) — every one of these has a deterministic, non-LLM fallback, so reasoning degrades to rules rather than stopping |
| **Memory — short-term** | State carried through one run | The JSON artifacts each phase hands to the next (`scraped_jobs.json` → `evaluated_jobs.json` → `qualified_jobs.json` → tailored PDFs → `application_results.json`), held only for the run's duration |
| **Memory — long-term** | State that outlives a run | `data/profiles/profile.json` (the sealed candidate identity), `preferences.json` (stated eligibility/salary, reapplied on every future intake), `delta_store.db` (seen-job/application/outreach history — nothing is ever "new" twice), `jobs.db` (the full queryable history of everything fetched, scored, tailored, applied, replied to), `inbox_events.json` and `evaluation_checkpoint.json` (resume-after-interruption checkpoints keyed to the exact profile+batch, so an interrupted run picks up mid-batch instead of re-paying for work already done) |
| **Plan** | Deciding what sequence of phases to run and in what order | `workflow.py` (the shared orchestrator every entry point — CLI, dashboard, `daily`, `repair` — calls into with an explicit phase list); inside a single application, `automation/agent.py`'s step loop plans one DOM action at a time, bounded, rather than planning the whole form up front (the page can change based on what was just filled) |
| **Act** | Doing something with an external, often irreversible, effect | `automation/form_filler.py` + `agent.py` (filling and submitting a real form), `tracking/tracker.py` (writing the Excel record), cold-email **drafting** (`tracking/cold_email.py`) — deliberately stops short of sending |
| **Observe** | Checking what actually happened after acting, and feeding it back in | Per-action: confirmation-text detection after submit, `ApplicationOutcome` recording. Per-run: `python main.py quality`/`performance`/`db audit` (self-observation of the system's own output — profile integrity, latency, cross-artifact consistency). Across runs: `sync-inbox` observing real-world outcomes (rejections, interviews) that resulted from a past `Act`, closing the loop from "applied" to "what happened" |

## What's genuinely partial, stated plainly

- **Observation doesn't yet trigger automatic re-planning.** `quality`/`performance`/`audit` produce a report a human reads; nothing currently says "performance report shows evaluation is the bottleneck, so automatically cap the next run's `--limit`." That's a deliberate boundary today, not a bug — an unattended agent silently changing its own scope is exactly the kind of thing this project's human-in-the-loop philosophy avoids. If this ever changes, it should be opt-in and logged, the same way `LLM_STRICT` and `--strict-llm` are explicit opt-ins rather than silent defaults.
- **Long-term memory is single-machine.** `delta_store.db`/`jobs.db` are local SQLite files (Postgres-capable via `DATABASE_URL`, see `docs/DEPLOYMENT.md`, but not synced anywhere by default). Fine for one candidate on one machine; a multi-device or multi-user version needs that migration done first, not assumed.
- **CAPTCHA solving is stubbed, permanently, on purpose** (`automation/hitl.py`) — it always falls through to a human-in-the-loop pause rather than attempting to solve it autonomously. This is a guardrail choice, not a missing feature: autonomously defeating a bot challenge is the kind of capability this project deliberately doesn't build.

## Guardrails

Grouped by what they protect, each with the actual mechanism, not just the
intent:

**Content fidelity** (the project's core rule: no fact not in the resume)
- SHA-256 fact seal over locked metrics (`config/schema.py`'s `compute_fact_hash`); phases 4–7 refuse to run against a profile that fails `verify_integrity()`.
- Restore-or-drop gate on every rewrite (`tailoring/rewriter.py`'s `enforce_metric_integrity`): a dropped locked metric is reinstated, an invented one is stripped, and both are logged to the manifest.
- The same gate is reused, not reimplemented, by interview prep and cover letters via `generation.py`'s `verified_evidence` — a new text-generating feature that doesn't route through this gate would be a regression.
- Form fields with no backing fact are left blank and reported, never guessed (`automation/form_filler.py`).

**Execution bounds** (this code submits real applications and reads a real inbox)
- `MAX_APPLICATION_STEPS` (default 25) and `MAX_CONSECUTIVE_ERRORS = 3` bound the auto-apply loop (`automation/agent.py`).
- Submit is clicked at most once per job, and only on a page with a real form (file input, or name+email) — an "Apply" link alone doesn't count.
- Demographic/voluntary-disclosure fields (race, veteran status, disability, gender) are refused on every channel, including Workday assist, regardless of what data exists.
- `workday-assist` fills but architecturally cannot submit — it breaks out of the loop the moment a submit control is found.
- Dry-run is the default everywhere; live submission requires an explicit `--live`/`confirm_live=APPLY` field, so no single click or flag typo submits anything.
- `sync-inbox` is read-only, opt-in (all four IMAP env vars required), and never sends mail; ambiguous job/reply matches are left unmatched rather than guessed.
- `contacts`/warm-lead discovery never guesses an email, never crawls social platforms, and labels every lead "unverified."

**Consistency and concurrency**
- `runtime.pipeline_lock` / `@exclusive_run` serialize artifact-writing stages across CLI and dashboard processes, so two runs can't interleave writes to the same files.
- `invalidate_after` archives downstream artifacts when an upstream stage produces a new batch, so a later stage can't silently consume results built from a superseded input.
- Checkpointed LLM stages resume only against the identical profile content and judge — a changed profile or provider invalidates the checkpoint rather than reusing stale scores.

**Access and privacy**
- The dashboard binds to loopback only, full stop (`web/server.py`'s `run_server` hard-refuses any other host) — reaching it remotely goes through a private tunnel plus an explicit login (`DASHBOARD_USERNAME`/`PASSWORD`, `DASHBOARD_ALLOWED_HOSTS`), never a relaxed bind.
- Per-session CSRF token, Host/Origin DNS-rebinding checks, and a hard 25MB/1MB request-size cap on every mutating endpoint.
- The one place in the codebase that fetches an arbitrary caller-supplied URL from scrape/feed data (`sourcing/details.py`'s detail-page fetch) resolves the hostname first and refuses anything that isn't a public, routable address — the same check `contacts/finder.py` already used for employer-site crawling, reused rather than reinvented.
- `.env`, `data/profiles/`, and real resumes are gitignored; `ANONYMIZED_TELEMETRY` is forced off in-process regardless of what any dependency tries to set.

## Using this document

If you're adding a feature: find the row/section closest to what you're
building, and match its pattern rather than inventing a new one — a new
text-generator should call into `generation.py`'s gate the way interview prep
and cover letters do; a new external fetch should get the same public-address
check `details.py`/`finder.py` already share; a new destructive action should
default to dry-run and require explicit confirmation the way `apply` does.

If you're auditing this project (for yourself, a reviewer, or another agent
picking up the work): every claim above names the file and function it comes
from — re-verify it against the current source before trusting it, the same
standard `docs/ARCHITECTURE.md` holds itself to.
