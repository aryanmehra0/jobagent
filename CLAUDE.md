# CLAUDE.md

Read this first, every session. It is the orientation layer for any AI coding
agent working in this repository — Claude, Codex, Gemini, or otherwise. For
the full, file-by-file architecture map, read
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) next; don't duplicate it here,
and don't trust it blindly either — verify a path exists (`find src/job_agent
-name "*.py"`) before citing it, the way this file itself was built. A stale
version of `ARCHITECTURE.md` once named four files that didn't exist; nobody
caught it until someone actually ran `find`. For how the system maps onto the
Perceive/Reason/Memory/Plan/Act/Observe agent loop and the full guardrail
list, read [`docs/AGENT_DESIGN.md`](docs/AGENT_DESIGN.md) — match its
patterns (which gate a new feature routes through, which bound a new action
respects) instead of inventing a new one.

**Check whether this repository's GitHub remote is public before assuming
either way** (`gh repo view --json visibility`, or the visibility field from
the GitHub API for whatever `git remote get-url origin` currently points at —
the account and repo name have changed before and will likely change again).
Treat it as public unless you've just confirmed otherwise: never commit real
personal data — the candidate's name, email, resume, sealed profile, API
keys, or Tailscale/dashboard credentials. `.gitignore` already protects
`.env`, `data/profiles/`, and `data/raw_resumes/*.pdf` (except the bundled
fictional `sample_resume.pdf`) — keep it that way. If you're ever about to
write a real name, email, or key into a file that will be committed, stop and
use a placeholder instead.

## What this project is

A local, seven-stage pipeline that turns a candidate's resume into a
cryptographically sealed profile, sources and scores real job postings,
tailors a resume and cover letter per role, auto-applies where it safely can,
tracks outcomes, and drafts interview prep.

**The one rule everything else serves**: no stage may assert a fact about the
candidate that isn't in their resume. Three gates enforce it end to end —
intake (`intake/validator.py`, SHA-256 fact seal), tailoring
(`tailoring/rewriter.py`, restore-or-drop unverified metrics), and form
filling (`automation/form_filler.py`, blank rather than guessed). Every new
feature that touches candidate-facing text (interview prep, cover letters,
warm-contact leads) reuses this same gate via `generation.py`'s
`verified_evidence`/`enforce_metric_integrity` rather than inventing its own
trust logic. Don't weaken this for convenience, and don't add a new
text-generating feature without routing it through the existing gate.

Everything works with zero API keys — every LLM-backed stage has a
deterministic offline fallback. Don't make that stop being true.

## Where things live

```
job agent/
├── README.md          # Day-to-day usage; the entry point for a human
├── CLAUDE.md           # This file
├── docs/               # Everything else: START_HERE, DEPLOYMENT, VALIDATION,
│                        # ARCHITECTURE, ROADMAP_IMPLEMENTATION, IMPROVEMENT_ROADMAP
├── scripts/            # start_dashboard.ps1, install_autostart.ps1,
│                        # validate_live_pipeline.py, and other operator scripts —
│                        # not imported by the package, run directly
├── src/job_agent/       # The package (~80 .py files) — see docs/ARCHITECTURE.md
│                        # for the verified full tree; top-level modules worth
│                        # knowing up front: cli.py (27 commands + a db group),
│                        # workflow.py (shared pipeline runner), generation.py
│                        # (shared anti-hallucination evidence helpers), llm.py,
│                        # runtime.py (cross-process lock, @exclusive_run),
│                        # storage/jobs_db.py + storage/migrations.py
│                        # (SQLite/Postgres jobs store and explicit schema ledger)
├── tests/               # 31 files; run with pytest, see "Testing" below
└── data/                # Gitignored runtime data (profile, resumes, outputs, DBs)
```

## Testing — do this before claiming anything works

```powershell
python -m pytest -q
```

That alone isn't enough — **run it two ways**, because they've disagreed
before and the difference matters:

```powershell
# Matches .github/workflows/ci.yml exactly:
$env:DEFAULT_LLM_PROVIDER='none'; $env:JOB_AGENT_LOAD_DOTENV='0'; $env:ANONYMIZED_TELEMETRY='false'
python -m pytest -ra --ignore=tests/test_browser_integration.py
```

CI starts a disposable PostgreSQL 16 service and sets
`JOB_AGENT_POSTGRES_TEST_URL`, so `tests/test_postgres_integration.py` runs for
real in GitHub Actions instead of silently skipping. That test covers the jobs
DB, DeltaStore, artifact verification, run events, hosted queue, and hosted
auth tables on one Postgres schema. Locally, start Postgres and set that env
var to run the same coverage; without it the test stays opt-in for developers
who do not have Docker/Postgres running.

Why both: a real `.env` on the machine running tests (e.g. a configured
`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`) can make plain `pytest` fail in
ways the CI invocation never sees, or vice versa. `tests/conftest.py`'s
autouse `isolate_llm_settings` fixture exists specifically to neutralize
real-`.env` settings for tests — **extend it whenever you add a new
`Settings` field that a real `.env` might set**, or you'll reproduce the exact
bug this comment is describing.

After every push, **confirm CI actually passed** — don't assume from "it
pushed cleanly". Derive the repo from the actual remote rather than
hardcoding an owner/name that will go stale the next time it changes:

```bash
REMOTE=$(git remote get-url origin | sed -E 's#.*[:/]([^/]+/[^/.]+)(\.git)?$#\1#')
curl -s "https://api.github.com/repos/$REMOTE/actions/runs?per_page=1"
```

Unit tests aren't the ceiling either. This repo has a live, isolated,
end-to-end check that hits real Greenhouse listings and real Groq calls
without touching the operator's actual resume/profile:

```powershell
python scripts/validate_live_pipeline.py
```

Use it (or `python main.py doctor` / `production-check` / a live `curl` to a
running dashboard) whenever a change touches sourcing, evaluation, tailoring,
or the web server — reading the diff is not the same as proving it runs.

## Hard-won gotchas (don't rediscover these)

- **Git Bash mangles bare `/` arguments** into Windows paths (e.g.
  `tailscale serve https / http://...` becomes garbage). Use the PowerShell
  tool for Tailscale/Windows-native commands, not the Bash tool.
- **Windows Task Scheduler's "restart on failure" is unreliable** for
  logon-triggered tasks — tested directly (killed the process, configured
  `RestartCount`/`RestartInterval` correctly, it did not restart after
  150+ seconds). `scripts/start_dashboard.ps1` has its own internal retry
  loop because of this; don't add a feature that depends on Task Scheduler's
  restart mechanism alone.
- **`choose_resume()` (`intake/preferences.py`) scans the shared
  `data/raw_resumes/` directory**, not whatever `outputs_dir`/`profile_path`
  a script has overridden. Any script that needs to force demo-resume
  behavior on a machine that already has a real resume uploaded must isolate
  `settings.raw_resumes_dir` too, or the demo-vs-real guard in
  `tailoring/pipeline.py` will (correctly, by design) refuse to proceed.
- **The demo-vs-real-resume guard existing at all is a feature, not a bug** —
  it stops the pipeline from ever tailoring/sending documents built from the
  bundled fictional resume once a real one is uploaded. If you hit it
  unexpectedly, fix your script's isolation, don't remove the guard.
- **The jobs database is in a compatibility migration, not a clean-slate ORM.**
  `JobsDatabase` dual-writes legacy tables (`jobs`, `job_evaluations`,
  `job_applications`, `job_resumes`) for the existing CLI/dashboard, and the
  normalized tables (`users`, `candidate_profiles`, `candidate_preferences`,
  `job_matches`, `job_evaluation_history`, `applications`,
  `application_events`, `job_source_listings`, `resume_artifacts`) for the
  target Postgres-first architecture. Run `python main.py db migrations` to
  inspect the schema ledger. Prefer new reads from `job_overview` or the
  normalized tables, not directly from `jobs.fit_score` or `jobs.status`.
- **CSV/XLSX/JSON are becoming exports, not authority.** Existing phases still
  write artifacts, and sync still backfills from them, but exports overlay DB
  state and can rebuild from the database when artifacts are absent. Evaluation loads
  `scraped_jobs.json` when present, but falls back to normalized
  `job_source_listings`/`jobs` through `JobsDatabase.source_jobs()` when the
  source hand-off file is missing. Apply also loads
  `tailored_resumes/manifest.json` when present, but can rebuild the manifest
  from normalized `resume_artifacts`; the application-pack ZIP builder uses the
  same fallback. Tailoring, apply, interview prep, and fallback tracking load
  `qualified_jobs.json` when present, but fall back to
  normalized `job_matches`/`job_evaluation_history` through
  `JobsDatabase.evaluated_jobs()` when that hand-off file is missing. The
  dashboard source/evaluation/tailoring/application state and `python main.py
  status` also use normalized DB fallbacks for source/evaluation/tailoring/
  application summaries, including sourced jobs when `scraped_jobs.json` is
  absent. Run-history job linking also falls back to DB source/evaluation/
  resume-artifact state when per-run artifacts are missing, and tracker resume
  checks fall back to normalized resume artifacts when the tailoring manifest is
  absent, and interview prep / cover-letter document links fall back to
  `interview_prep_artifacts` / `cover_letter_artifacts` when those manifests
  are absent. Source, evaluation, tailoring, apply, tracking, and interview
  prep now refresh `JobsDatabase` immediately after writing their compatibility
  artifacts, so normalized DB state is current during those phases instead of
  waiting for the final export/publish step.
  New backend work should keep moving read paths to the database while
  preserving artifact compatibility.
- **Global job state is now separate from candidate/application state.** New
  syncs keep `jobs.status` to job lifecycle values such as `active`, while
  candidate-stage state lives in `job_matches.state` and application-stage
  state lives in `applications.current_status`. `job_overview.status` is still
  the compatibility projection the CLI/dashboard should read when it wants a
  human stage label. Run `python main.py db integrity` after storage changes;
  `candidate_states_in_jobs_status` must stay at `0`.
- **Deduplication is layered.** `job_fingerprint()` remains the broad
  company/title role key used for outreach compatibility, but ingestion skip
  decisions use `DeltaStore.processing_fingerprint(job)`, which includes
  non-remote location evidence. Do not replace it with company+title only, or
  same-title roles in different cities can disappear.
- **`DeltaStore` is local SQLite only when no `DATABASE_URL` is set.** Hosted
  mode now uses Postgres for `seen_jobs`, `application_attempts`,
  `outreach_log`, and `delta_meta`, so duplicate-prevention state lives beside
  jobs/queue state instead of in a separate `delta_store.db`.
- **Hosted queue rows are recoverable.** `HostedQueue` tracks attempts,
  `claimed_at`, `started_at`, `heartbeat_at`, and `finished_at`; use
  `python main.py queue status` and `python main.py queue recover-stale`
  before/inside workers to inspect or move abandoned running work to `retryable`
  or terminal `failed`. Run creation accepts an `Idempotency-Key` header (or
  `idempotency_key` payload field) and enforces one run per user/key in SQLite
  and Postgres, so API retries should not create duplicate hosted work.
- **Hosted workers are validation-first and execution is explicit.**
  `hosted/worker.py` completes validation rows by default. With
  `HOSTED_WORKER_EXECUTE=1`, it runs phases in a child process with per-user
  data/output/artifact/browser-profile paths under
  `HOSTED_WORKER_WORKSPACE_DIR`. Live apply still requires both queued
  `allow_live_apply=true` and `HOSTED_WORKER_ALLOW_LIVE_APPLY=1`; do not
  weaken that two-key gate.
- **Generated documents have an artifact-storage boundary.** Local development
  stores copied objects under the current outputs folder's `artifacts/`
  directory while `resume_artifacts.file_path` remains the compatibility path
  to the generated PDF. Production can set `ARTIFACT_STORAGE_BACKEND=s3` with
  `ARTIFACT_S3_BUCKET`/`ARTIFACT_S3_ENDPOINT_URL`; the database keeps
  `storage_backend`, `object_key`, `mime_type`, `sha256`, and size metadata.
  `production-check` intentionally flags local artifact storage in
  `staging`/`production`; local disk is only a smoke-test/developer default.
  Use `python main.py db artifacts verify` after backup/restore or S3 changes
  to prove resume object bytes, interview-prep guide files, and cover-letter
  PDFs still match database hashes.
- **Structured observability lives in `run_events`.** Phase records now emit an
  append-only event, and Groq calls record safe LLM metadata
  (`provider`/`model`, latency, token estimates, prompt/response hashes) without
  raw prompts or resume text. Use `python main.py db runs` for database-backed
  run history and `python main.py db events --phase llm` or filter by
  `--run-id` for operational debugging.
- **Hosted API responses are production-shaped.** Error responses use stable
  `{error: {code, message}}` payloads and every response carries
  `X-Request-ID`. The production-facing hosted control plane is
  `job_agent.hosted.fastapi_app:app` under uvicorn, with typed `/v1/...`
  routes and OpenAPI; the older standard-library `hosted/api.py` remains only
  as a compatibility smoke path. `/v1/jobs` is user-scoped and
  cursor-paginated with `limit` and `after`, returning `jobs` plus
  `next_cursor`. Browser access is restricted by
  `HOSTED_API_ALLOWED_ORIGINS`, and authenticated control-plane routes have a
  fixed-window in-process rate limit via `HOSTED_API_RATE_LIMIT_PER_MINUTE`.
- **Production/staging require Postgres.** `JOB_AGENT_ENVIRONMENT=staging` or
  `production` refuses implicit SQLite fallback for the main jobs DB,
  `DeltaStore`, and the hosted queue unless `DATABASE_URL` is set. Explicit
  test/local `db_path=` remains supported.
- **`production-check` is an operational gate, not just a static file check.**
  It now verifies migration status, DB integrity, hosted queue access, artifact
  storage configuration, and Postgres CI wiring in addition to profile/search
  readiness and deployment files.
- **The dashboard binds to loopback only, and must keep doing so**
  (`web/server.py`'s `run_server` hard-refuses any other host). To reach it
  remotely, the answer is a private tunnel with a login
  (`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`, `DASHBOARD_ALLOWED_HOSTS`), see
  `docs/DEPLOYMENT.md` — never relax the loopback bind itself.
- **A file existing check tied to a doc's location will break silently if you
  move the doc.** `cli.py`'s `production-check` once checked for
  `DEPLOYMENT.md` at the repo root; moving docs into `docs/` without updating
  it would have made that check start reporting "missing" for no visible
  reason. Grep for a filename before moving or renaming anything referenced
  by path in code.

## Current completion handoff

The backend/platform completion pass has moved the repo well beyond the
original local-only prototype, but it is still a reference hosted architecture
until deployed with managed infrastructure.

Implemented and verified in this pass:

- Normalized job/application status separation: `jobs.status` is global job
  lifecycle, candidate-stage state lives in `job_matches.state`, and
  application-stage state lives in `applications.current_status`.
- Explicit DB migrations through migration **8/8**, including
  `interview_prep_artifacts` and `cover_letter_artifacts`.
- Postgres-aware jobs DB, `DeltaStore`, and hosted queue; staging/production
  refuse implicit SQLite fallback without `DATABASE_URL`.
- Source, evaluation, tailoring, apply, tracking, interview prep, and cover
  letter phases refresh `JobsDatabase` after writing compatibility artifacts.
- Resume, interview-prep, and cover-letter generated documents now have
  database metadata and hash verification through
  `python main.py db artifacts verify`.
- Hosted queue rows are retryable/recoverable with attempts, heartbeats,
  stale-run recovery, and idempotent enqueue keys.
- Hosted FastAPI control plane exists at `job_agent.hosted.fastapi_app:app`
  with typed `/v1/...` routes, bearer auth, ownership filtering, request IDs,
  stable error envelopes, CORS, rate limiting, OpenAPI, and tests.
- Hosted worker is validation-first by default and only executes the pipeline
  when `HOSTED_WORKER_EXECUTE=1`; live apply also requires
  `HOSTED_WORKER_ALLOW_LIVE_APPLY=1` plus the queued run's
  `allow_live_apply=true`. In `staging`/`production`, live apply also requires
  `HOSTED_WORKER_ISOLATION_MODE=container`, so a public worker fails closed if
  it is still using the local workspace-only isolation mode.
- Hosted worker subprocesses set `JOB_AGENT_HOSTED_USER_ID`; `JobsDatabase`
  uses it as the owning `users.user_id` and scopes candidate-owned normalized
  rows with a filesystem-safe hosted prefix, while local runs keep the legacy
  email/profile-hash candidate ID behavior. Candidate-scoped read helpers
  (`jobs`, `evaluated_jobs`, `application_stats`, `tailored_resumes`,
  interview-prep artifacts, and cover-letter artifacts) also default to that
  hosted candidate context when the worker env is active, so shared Postgres
  fallback reads do not accidentally use another candidate's latest
  match/application rows.
- Successful executed hosted worker runs publish the completed workspace's
  DB-backed job rows into `hosted_user_jobs`, which is what `/v1/jobs` reads.
  This means the hosted API no longer needs manually seeded fixture rows to show
  a user's own completed run results.
- Hosted worker lifecycle observability now writes best-effort `run_events`
  with run IDs like `hosted:<id>` for validation, execution start, job
  publication, success, and failure. Telemetry write failures are swallowed so
  they do not mask the real queue transition.
- Hosted run events are API-visible through owner-scoped
  `GET /v1/runs/<id>/events` (and compatibility `/runs/<id>/events`); event
  rows are filtered to the authenticated hosted user.
- Hosted run listing is API-visible through owner-scoped `GET /v1/runs` (and
  compatibility `/runs`) with optional `status` and `limit` filters; filtering
  happens in `HostedQueue.list_runs(user_id=...)`, not after a global limited
  query.
- Structured operational events live in `run_events`; Groq calls record safe
  metadata without prompts or resume text.
- CI has a Postgres 16 service and `JOB_AGENT_POSTGRES_TEST_URL`; local
  Postgres tests skip unless that env var is set.

Verification recorded on 2026-10-05:

- Focused document artifact tests passed:
  `tests/test_jobs_db.py::test_cover_letter_artifacts_are_database_backed`,
  `tests/test_jobs_db.py::test_document_links_fall_back_to_database_cover_letters`,
  `tests/test_jobs_db.py::test_artifact_integrity_report_flags_cover_letter_hash_mismatch`.
- `tests/test_postgres_integration.py` skipped locally because
  `JOB_AGENT_POSTGRES_TEST_URL` was unset; Docker Desktop was not available in
  this environment, so rely on CI or a local Postgres instance for real
  Postgres execution.
- `python main.py production-check` passed with DB migrations **8/8**, clean
  DB integrity, hosted API/worker checks, and artifact verification.
- `python main.py db artifacts verify --limit 10` passed.
- Full non-browser pytest passed:
  `877 passed, 1 skipped, 1 warning in 169.32s`; the skip was the expected
  opt-in real Postgres test because `JOB_AGENT_POSTGRES_TEST_URL` was unset.
- Additional hosted identity isolation slice added afterward: same resume
  email across different hosted accounts now creates separate normalized
  candidate IDs, and candidate-scoped overview reads return the requested
  candidate's match/application state. Verified with focused
  `tests/test_jobs_db.py`, `tests/test_hosted_control_plane.py`, and
  `tests/test_hosted_fastapi.py`, then a fresh full non-browser pytest:
  `878 passed, 1 skipped, 1 warning in 191.66s`; the skip was again the
  expected opt-in real Postgres test because `JOB_AGENT_POSTGRES_TEST_URL` was
  unset.
- Worker-to-hosted-API job publishing added afterward and verified with
  `python -m pytest tests/test_hosted_control_plane.py -q` (`16 passed`), a
  broader hosted/DB focused pytest selection (`25 passed, 1 warning`),
  `production-check`, `db integrity`, and a fresh full non-browser pytest:
  `879 passed, 1 skipped, 1 warning in 191.75s`; the skip was again the
  expected opt-in real Postgres test because `JOB_AGENT_POSTGRES_TEST_URL` was
  unset.
- Hosted worker lifecycle `run_events` added afterward and verified with
  `python -m pytest tests/test_hosted_control_plane.py -q` (`16 passed`), a
  hosted/observability focused selection (`25 passed, 1 warning`),
  `production-check`, `db integrity`, and a fresh full non-browser pytest:
  `879 passed, 1 skipped, 1 warning in 198.33s`; the skip was again the
  expected opt-in real Postgres test because `JOB_AGENT_POSTGRES_TEST_URL` was
  unset.
- Hosted run events API exposure added afterward and verified with
  `python -m pytest tests/test_hosted_fastapi.py tests/test_hosted_control_plane.py -q`
  (`24 passed, 1 warning`), `production-check`, `db integrity`, and a fresh
  full non-browser pytest: `880 passed, 1 skipped, 1 warning in 190.78s`; the
  skip was again the expected opt-in real Postgres test because
  `JOB_AGENT_POSTGRES_TEST_URL` was unset.
- Hosted owner-scoped run listing added afterward and verified with
  `python -m pytest tests/test_hosted_fastapi.py tests/test_hosted_control_plane.py -q`
  (`25 passed, 1 warning`), `production-check`, `db integrity`, and a fresh
  full non-browser pytest: `881 passed, 1 skipped, 1 warning in 177.67s`; the
  skip was again the expected opt-in real Postgres test because
  `JOB_AGENT_POSTGRES_TEST_URL` was unset.
- Hosted production live-apply isolation was hardened afterward:
  `HOSTED_WORKER_ALLOW_LIVE_APPLY=1` is not enough in `staging`/`production`
  unless `HOSTED_WORKER_ISOLATION_MODE=container` is also set; invalid
  isolation-mode env values are rejected before a subprocess starts. Verified
  with focused worker/production-check tests (`4 passed`), broader
  hosted/workflow tests (`44 passed`), `production-check`, `db integrity`, and
  a fresh full non-browser pytest: `885 passed, 1 skipped, 1 warning in
  196.30s`; the skip was again the expected opt-in real Postgres test because
  `JOB_AGENT_POSTGRES_TEST_URL` was unset.

Known remaining boundaries before calling this a public hosted SaaS:

- Deploy a real Postgres database and set `DATABASE_URL` in staging/production.
- Decide whether the relational `HostedQueue` is enough or replace it with a
  managed queue for high scale.
- Add deployed per-user container/browser isolation before enabling public
  multi-user live apply.
- Configure real production artifact storage credentials/bucket
  (`ARTIFACT_STORAGE_BACKEND=s3` plus bucket/endpoint settings); the readiness
  gate now fails closed on local artifact disk in staging/production, but it
  cannot prove external bucket access without real credentials.
- Run the Postgres integration suite against a real Postgres service after
  infrastructure is available.

## Working conventions for this repo

- Git commits end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Prefer new commits over amending; one focused commit per real change, with a
  commit message that explains *why*, including what was verified and how —
  look at recent `git log` output in this repo for the expected level of
  detail. A commit message that just restates the diff is not enough here.
- Documentation in this repo is written the same way the code enforces
  honesty: no claim without a check behind it. Don't write "this validates X"
  unless you ran it and saw X validated. Prior sessions have both fabricated
  file paths in docs and fixed the fabrication later — the fixing is the
  standard to hold yourself to from the start, not just when caught.
- Keep the deterministic-fallback and local-first invariants intact when
  adding features: a new capability should degrade gracefully without API
  keys, and should not require exposing anything beyond loopback by default.

## Who this is for

One real candidate's personal job-search tool, actively used with their own
resume and data (kept local, gitignored, never described by name in anything
committed). It is not a team codebase with multiple contributors coordinating
— it's a single person directing an AI agent to build and operate their own
tool, end to end, including the operational side (Windows scheduled tasks,
Tailscale, PowerShell) that they are not deeply familiar with themselves. Two
things follow from that:

1. **Verify operationally, not just in code.** When a fix touches something
   the user runs on their machine (the dashboard, a scheduled task, a
   tunnel), test it the way they'd hit it — curl the real URL, kill the real
   process and watch it recover, not just "the script looks right."
2. **They want this to stop needing hands-on-keyboard maintenance.** Prefer
   the self-healing, automated version of a fix over one that requires them
   to remember a manual step, and say clearly which parts of a setup are a
   one-time action only they can do (anything tied to their personal
   account/identity — signing into Tailscale, approving an OAuth screen) versus
   what you can automate for them.
