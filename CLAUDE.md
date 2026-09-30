# CLAUDE.md

Read this first, every session. It is the orientation layer for any AI coding
agent working in this repository — Claude, Codex, Gemini, or otherwise. For
the full, file-by-file architecture map, read
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) next; don't duplicate it here,
and don't trust it blindly either — verify a path exists (`find src/job_agent
-name "*.py"`) before citing it, the way this file itself was built. A stale
version of `ARCHITECTURE.md` once named four files that didn't exist; nobody
caught it until someone actually ran `find`.

**This repository is public on GitHub** (`github.com/aryanmehra0/jobagent`).
Never commit real personal data: the candidate's name, email, resume, sealed
profile, API keys, or Tailscale/dashboard credentials. `.gitignore` already
protects `.env`, `data/profiles/`, and `data/raw_resumes/*.pdf` (except the
bundled fictional `sample_resume.pdf`) — keep it that way. If you're ever
about to write a real name, email, or key into a file that will be committed,
stop and use a placeholder instead.

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
│                        # runtime.py (cross-process lock, @exclusive_run)
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

Why both: a real `.env` on the machine running tests (e.g. a configured
`DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD`) can make plain `pytest` fail in
ways the CI invocation never sees, or vice versa. `tests/conftest.py`'s
autouse `isolate_llm_settings` fixture exists specifically to neutralize
real-`.env` settings for tests — **extend it whenever you add a new
`Settings` field that a real `.env` might set**, or you'll reproduce the exact
bug this comment is describing.

After every push, **confirm CI actually passed** — don't assume from "it
pushed cleanly":

```bash
curl -s 'https://api.github.com/repos/aryanmehra0/jobagent/actions/runs?per_page=1'
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
