# Improvement Roadmap — Ready-to-Paste Prompts

Generated 2026-09-21 after a full read of the codebase (ARCHITECTURE.md, DEPLOYMENT.md,
VALIDATION.md, and the source tree) plus a market scan of 2026 AI job-application
tools (Jobright, Simplify, LazyApply, JobCopilot, OutApply, Teal, Huntr — see
Sources at the bottom).

## How to use this file

Each numbered item below is self-contained. Copy the "Prompt" block for the item
you want and paste it as-is into a new Claude Code session in this repo. It
already contains the context a fresh session needs (why, where, constraints),
so you don't have to re-explain the project. Do them roughly in order — later
items assume earlier ones exist (e.g., analytics needs reply-tracking data).

---

## Where this project already beats the market

Worth knowing before you prioritize, so nobody "fixes" these by accident:

- **Anti-hallucination fact-sealing** (SHA-256 locked facts, restore/fabrication
  gates in `rewriter.py`) — no competitor found in the research does this. Most
  AI resume tools invent metrics. This is the project's strongest differentiator
  and should be advertised, not diluted.
- **Faithful in-place PDF tailoring** (`tailoring/faithful.py`) — reorders blocks
  inside the candidate's own PDF instead of regenerating layout, so formatting,
  fonts, and page count never drift. Jobright/Simplify/LazyApply all regenerate
  from a template and lose the original design.
- **Zero-key deterministic fallback** — the entire six-stage pipeline runs
  without any LLM key. No paid competitor offers a free, fully offline mode.
- **Local-first privacy** — loopback-only dashboard, no data leaves the machine
  unless the user configures a provider. Competitors are all SaaS with your
  resume on their servers.
- **Visual flow console** with SSE-driven live pipeline graph — most competitors
  are a plain job list with a button.

Gaps versus the market leaders, researched below, are what this roadmap targets.

---

## 1. Interview prep copilot (Phase 7)

**Why**: Every top-ranked 2026 tool (Jobright, Huntr, Final Round AI, Google
Interview Warmup) now pairs auto-apply with interview prep. This project stops
at "applied" — it has no answer for "now what." It's also a natural extension
of data already collected: `qualified_jobs.json` has the JD, `profile.json` has
locked facts, so likely questions and STAR-format answers can be grounded in
real experience instead of invented ones (same anti-hallucination principle
that already differentiates tailoring).

**Prompt**:
```
Add a Phase 7 "Interview Prep" stage to this job agent, following the same
architectural pattern as the other phases (see ARCHITECTURE.md sections 7-12
for the pattern: a pipeline.py orchestrator, an LLM call with a deterministic
heuristic fallback, Pydantic schema additions in config/schema.py, and a CLI
subcommand in cli.py).

For each job in qualified_jobs.json (or applications_ready), generate:
1. 8-12 likely interview questions derived from the JD's stated requirements,
   split into technical / behavioral / company-fit buckets.
2. A draft STAR-format answer for each behavioral question, built ONLY from the
   candidate's locked_facts and experience bullets in profile.json — reuse the
   existing anti-hallucination gate pattern from tailoring/rewriter.py
   (enforce_metric_integrity) so no answer states a metric or claim not already
   in the sealed profile.
3. A one-page "company + role" briefing: what the JD emphasizes, likely panel
   composition guesses, and 3 smart questions to ask the interviewer.

Write output to data/outputs/interview_prep/<job_id>.md (readable, sendable to
a phone) and add a summary to the existing tracker/dashboard. Add a `prep`
CLI command and wire it into the dashboard's job detail view (web/state.py,
web/static/app.js) the same way tailored PDFs are surfaced today. Add tests
mirroring tests/test_tailoring.py and tests/test_anti_hallucination.py:
confirm no generated answer contains a number/metric absent from the sealed
profile.
```

---

## 2. Inbound reply tracking (auto-detect rejections/interviews/offers)

**Why**: Right now `tracking/tracker.py` only updates status from the agent's
own actions (applied/dry-run). The candidate must manually mark rejections and
interview invites. Competitors like Teal/Huntr differentiate on tracking, and
this is the highest-leverage feature for measuring whether the whole pipeline
actually works (funnel: applied → response → interview → offer).

**Prompt**:
```
Add an opt-in email reply watcher that updates applications_master status
automatically. Context: DeltaStore (sourcing/delta_store.py) already tracks a
VALID_STATUSES lifecycle (scraped -> evaluated -> qualified -> tailored ->
applied/failed). Extend it with post-apply states: replied_rejection,
replied_interview, replied_offer, replied_other.

Design:
- New module src/job_agent/tracking/inbox.py using IMAP (imaplib, stdlib —
  match this project's existing pattern of stdlib-first, e.g. web/server.py
  uses http.server with zero framework deps) to read a folder the user points
  at (their own inbox, read-only, via app password — never a full mailbox
  scrape, never send).
- Match inbound emails to a tracked application by sender domain + company
  name + job title fuzzy match (there's already fuzzy company/title matching
  logic worth reusing in automation/routing.py or tailoring/rewriter.py's role
  matching — check before writing new matching code).
- Classify each matched email deterministically first (regex/keyword rules for
  "unfortunately", "not moving forward", "schedule a call", "offer") with an
  LLM classification fallback for ambiguous cases, following the same
  provider-then-heuristic pattern used in evaluation/reranker.py.
- Never auto-reply. This only reads and classifies.
- New CLI command `sync-inbox`, gated behind an explicit env var
  (IMAP_HOST/IMAP_USER/IMAP_APP_PASSWORD) so it's fully opt-in and off by
  default, consistent with this project's "everything works with zero keys"
  invariant (ARCHITECTURE.md section 15/18).
- Add tests with a fake IMAP mailbox fixture (mirror the fixture style in
  tests/test_contact_discovery.py) covering: rejection detected, interview
  detected, non-matching email ignored, already-classified email not
  reclassified (idempotent, like MasterTracker's idempotent rows).
```

---

## 3. Outcome analytics dashboard (funnel + what's actually working)

**Why**: Depends on #2. Once real outcomes exist, the dashboard should answer
"is this working?" — response rate by match-score bucket, by tailoring
variant, by source, by role title. This is the feature that turns the project
from "a scraper with a nice UI" into a system that gets measurably better over
time, and no auto-apply competitor in the research does per-user outcome
analytics well (most just show a static count of "applications sent").

**Prompt**:
```
Add an analytics view to the dashboard (web/static/app.js + a new
web/analytics.py reading data/outputs/jobs_master.csv and the reply states
from tracking/inbox.py, if #2 is implemented — otherwise fall back to
applied/not-applied only and say so in the UI).

Compute, without any new dependency (pure Python, matching this project's
minimal-deps philosophy — see ARCHITECTURE.md section 16):
- Funnel counts: sourced -> qualified -> tailored -> applied -> replied ->
  interview -> offer, as both counts and conversion rates between stages.
- Response rate bucketed by match-score decile (does a 9.0 fit score actually
  get more replies than a 7.5?).
- Response rate by job source (LinkedIn vs Greenhouse-direct vs Indeed, etc. —
  sourcing/ats_direct.py and sourcing/scraper.py already tag `source`).
- Median time-to-first-response.
Render as a new tab in the existing Inspector sidebar (see ARCHITECTURE.md
section 13 for the app.js structure: Details/Live Log/Settings tabs — add
"Analytics" alongside them). Use simple SVG/canvas, no charting library, to
stay consistent with the zero-framework frontend. Add a
GET /api/analytics endpoint in web/server.py following the existing endpoint
patterns (auth/CSRF handling already centralized there).
Add tests mirroring tests/test_web.py's state-snapshot tests.
```

---

## 4. Wider ATS auto-fill coverage (Workday, iCIMS, Taleo, SuccessFactors)

**Why**: `VALIDATION.md` documents that Greenhouse, Lever, and Ashby are
handled and Workday currently routes to manual apply. Workday alone is used by
a large share of enterprise job postings, so this is the single biggest
"applied automatically" volume gap versus JobCopilot/LazyApply, which both
advertise Workday support explicitly.

**Prompt**:
```
Extend automation/routing.py and automation/form_filler.py to auto-fill Workday
job application forms (read automation/routing.py first — it currently detects
"Login-walled boards and account-based systems" and routes Workday to manual;
see VALIDATION.md's "Apply routing and contact discovery" section for the exact
current behavior and why it was chosen).

Workday forms are multi-step (typically: My Information -> My Experience ->
Application Questions -> Voluntary Disclosures -> Review). Requirements:
- Detect Workday by URL pattern (myworkdayjobs.com) in routing.py, same style
  as the existing ATS detection.
- Extend navigator.py's DOM scanner to handle Workday's multi-page wizard
  (Next button between steps, not a single-page form) — reuse
  MAX_APPLICATION_STEPS and the existing step-bounded loop in agent.py, don't
  introduce a second stepping mechanism.
- Fill only fields backed by a locked fact or profile field, exactly like
  form_filler.py's existing rule — Gate 3 in ARCHITECTURE.md ("no guessing, no
  placeholders, no automatic Yes on screening questions") must hold for Workday
  identically to Greenhouse/Lever.
- Workday's "Voluntary Disclosures" (race, veteran status, disability) must
  always be left as "Decline to answer" / skipped — never auto-answered, even
  if a value exists somewhere, since these are legally sensitive and not
  employment facts. Add an explicit test asserting this.
- Still stop before final submit unless the existing require_apply_confirmation
  / dry-run gates are satisfied — no new submit path bypasses those.
- Validate read-only against 3-5 live Workday listings first (see
  scripts/check_live.py and scripts/validate_live_pipeline.py for the existing
  live-validation pattern) and record findings in VALIDATION.md the same way
  the Greenhouse/Lever/Ashby findings are documented, before enabling live
  submission.
```

---

## 5. Cover letters (with the same anti-hallucination gate as resumes)

**Why**: Still commonly requested by ATS forms, and cheap to add correctly
given the anti-hallucination machinery already exists — this project is
better positioned than competitors to do it *without inventing claims*, which
is the usual failure mode of AI cover letters.

**Prompt**:
```
Add optional cover-letter generation to Phase 4 (tailoring). Reuse, don't
duplicate: rewriter.py's enforce_metric_integrity gate, cold_email.py's
<200-word discipline and locked-facts-only prompt pattern (it's the closest
existing analog — read it first), and compiler.py's Typst compilation path
for a matching-styled PDF output.

- New template templates/cover_letter.typ mirroring resume.typ's ATS-safe,
  single-column style.
- New function in tailoring/rewriter.py or a new tailoring/cover_letter.py
  (your call — pick based on whether rewriter.py is already large enough to
  split; check its current line count first) that drafts a 3-paragraph letter
  grounded only in locked_facts + the JD's stated requirements.
- Run the letter through the same fabrication gate as bullets: any sentence
  asserting a number not in locked_facts is rejected/rewritten, not just
  flagged.
- Make it opt-in per run (--cover-letter flag / dashboard toggle) since it
  roughly doubles Phase 4's LLM calls and this project is careful about Groq's
  200k-token/day free tier (see VALIDATION.md "Groq free tier" section) —
  don't make it silently double token usage for users on the free tier.
- Tests mirroring test_faithful_tailoring.py and test_anti_hallucination.py.
```

---

## 6. Referral / warm-intro discovery

**Why**: Cold applications convert far worse than referred ones industry-wide,
and this is a feature none of the researched competitors do well (they focus
on volume of applications, not warm paths in). This project already has
contact-discovery infrastructure (`contacts/finder.py`, `contacts/extract.py`,
Hunter.io integration) that's a natural base to extend.

**Prompt**:
```
Extend src/job_agent/contacts/ to also surface potential warm-intro paths, not
just hiring-mailbox emails. Read contacts/finder.py and contacts/extract.py
first — reuse their "only published/found data, never guessed" invariant
(VALIDATION.md: "Only published addresses are kept, and each records its
source"). This feature must follow the identical trust rule: never fabricate
a person, never guess an email pattern for a named individual without
verifying it against a found source.

Scope for a first pass (keep it modest — this is high hallucination-risk
territory, be conservative):
- When a company site's team/about page lists named employees with public
  role titles (already something extract.py partially handles for hiring
  mailboxes), surface names + titles that match the target role's team
  (e.g., "Engineering" for an SWE role) as "possible warm contacts", clearly
  labeled as unverified leads requiring the candidate's own outreach — never
  auto-email these.
- Do NOT scrape LinkedIn (against their ToS and this project's existing
  automation only touches ATS/employer domains, not social platforms — keep
  it that way).
- Surface in the dashboard job detail view as a new "Possible contacts"
  section, separate from the existing verified hiring-email section so the
  confidence levels are never conflated.
- Tests mirroring test_contacts.py and test_contact_discovery.py, especially
  a test that unverifiable/ambiguous names are dropped rather than guessed.
```

---

## 7. CI pipeline (this is currently missing entirely)

**Why**: There is no `.github/workflows/` directory — 199+ tests exist and run
locally but nothing enforces them on push/PR. This is the single cheapest,
highest-confidence improvement in this list and should probably be done first,
before any of the feature work above, so every subsequent change is checked
automatically.

**Prompt**:
```
Add GitHub Actions CI for this repo (github.com/aryanmehra0/jobagent). Create
.github/workflows/ci.yml that, on push and pull_request:
1. Sets up Python 3.11 (match pyproject.toml's requires-python).
2. Installs requirements.txt (skip playwright browser install in the fast
   job — browser tests are opt-in via JOB_AGENT_BROWSER_TESTS=1 per
   VALIDATION.md's "Reproduce" section, so exclude tests/test_browser_integration.py
   from the default CI run and add a second optional job that installs
   `playwright install chromium` and runs them, allowed to be slower).
3. Runs `python -m pytest -ra` for the main job.
4. Runs any linter already configured in pyproject.toml (check for ruff/black
   config there first — don't add a new linter/formatter choice unasked).
5. Caches pip dependencies keyed on requirements.txt hash to keep runs fast.

Also add a status badge to README.md pointing at the workflow. Keep secrets
out of it entirely — nothing in this CI job should need OPENAI_API_KEY,
ANTHROPIC_API_KEY, or GROQ_API_KEYS, since ARCHITECTURE.md's zero-key
invariant means the full test suite already passes without them; verify that
assumption holds by running pytest locally with those env vars unset before
finalizing the workflow.
```

---

## 8. Public hosted deployment — finish the scaffold

**Why**: `DEPLOYMENT.md` already has an honest, detailed "What a public hosted
version needs" checklist (auth, per-user storage, queue, isolated browser
workers, secrets, throttles, observability) — it correctly says the current
hosted scaffold is a smoke test, not production. This item is "go implement
that checklist," using the document that's already in the repo as the spec, so
DO NOT re-derive the plan — DEPLOYMENT.md already has it.

**Prompt**:
```
Read DEPLOYMENT.md's full "What a public hosted version needs" section and
"Database choices" section end to end before writing any code — it already
specifies the target architecture (per-user Postgres tables, object storage
for files, queue-based execution, isolated browser workers, secret storage,
per-provider throttles, observability). Your job is to implement the first
concrete slice of that checklist against the existing scaffold in
src/job_agent/hosted/ (api.py, queue.py, worker.py) and
docker-compose.hosted.yml, not to redesign it.

Pick ONE slice to implement fully rather than partially touching all of them:
recommended first slice is real per-user auth (replace the single
HOSTED_API_TOKEN bearer check in hosted/api.py with per-user API keys or
OAuth, add a users table to the Postgres schema referenced in DEPLOYMENT.md,
scope /runs and job data by user_id which the API already accepts as a field
today — check hosted/api.py's current request schema first).

Keep the existing safety posture: HOSTED_WORKER_EXECUTE stays opt-in
(DEPLOYMENT.md: "Keep the reference worker in validation mode until you have
per-user storage and a per-user browser profile"), and don't wire real browser
automation into the hosted worker as part of this slice — that's explicitly
called out as needing isolated per-user containers first, which is a separate,
later slice.

Add tests mirroring tests/test_hosted_control_plane.py for the new auth model.
```

---

## 9. Resume/keyword gap analysis against the JD (pre-apply signal)

**Why**: Jobscan-style ATS keyword matching is one of the most-requested
standalone features in the research (several competitors bolt on a "resume
score" as their hook). This project already computes embedding similarity and
an LLM fit score (`evaluation/embedder.py`, `evaluation/reranker.py`) — this
is mostly a UI surface for data the pipeline already derives, not new scoring
logic.

**Prompt**:
```
Surface a "why this score" breakdown in the dashboard for each evaluated job,
using data the pipeline already computes — don't build a second scoring
system. Read evaluation/reranker.py's RerankerVerdict schema in
config/schema.py first: it already has matching_skills[] and missing_skills[].
This is currently computed but check whether it's fully surfaced in the
dashboard job detail view (web/static/app.js) — if it's already there, this
task is just improving the presentation (e.g., a clear "add these keywords"
callout), not adding new data.

If matching_skills/missing_skills aren't already rendered per-job in the UI,
add that view. If they are, instead add: a diff view showing which of the
missing_skills, if any, actually exist elsewhere in the candidate's profile
(skills section, other roles' bullets) but weren't surfaced to the reranker's
context window (reranker.py's select_context has a
CONTEXT_BUDGET_CHARS=6000 cap — check whether truncation is dropping real
matches, which would be a scoring-accuracy bug worth fixing, not just a UI
gap).
```

---

## Lower-priority / defer

These came up in research but are either high legal/ToS risk, high effort for
uncertain value, or premature before the items above:

- **LinkedIn Easy Apply automation** — LinkedIn's ToS actively prohibits this
  and several competitors have faced account bans doing it. Do not build this
  without the user explicitly accepting that risk in writing; if pursued,
  scope it as browser-assisted (user stays logged in, agent only navigates
  and pre-fills, never submits) not fully autonomous.
- **Mobile app / push notifications** — nice-to-have, but this is a local
  loopback-bound tool by design (DEPLOYMENT.md, ARCHITECTURE.md invariant 11);
  a mobile app implies the hosted path (#8) has to be production-solid first.
- **Salary negotiation assistant** — valuable but needs a market-data source
  (Levels.fyi-style) this project doesn't have and would need to license or
  scrape; flag as a "needs a data source decision" item, not a coding task.

---

## Sources consulted for the market scan

- [Jobright vs Simplify — Feature Comparison](https://jobright.ai/compare/simplify)
- [Top 5 LazyApply and Simplify Alternatives (2026)](https://dev.to/codev206/top-5-lazyapply-and-simplify-alternatives-for-higher-quality-ai-applications-2026-1pki)
- [Auto-Apply to Jobs Tools Compared 2026](https://blog.fastapply.co/auto-apply-jobs-tools-compared-2026)
- [9 Best AI Auto-Apply Tools in 2026](https://www.resumly.ai/best/best-ai-auto-apply-tools)
- [Tested 10 Job Tracker Apps Reviewed for 2026](https://scale.jobs/blog/tested-job-tracker-apps-reviewed)
- [Top-Rated AI Job Search Agents and Automation Tools 2026](https://tsenta.com/blog/top-rated-ai-job-search-agents-automation-tools)
- [How to Get Hired in 2026 (AI-Era Job Search Playbook)](https://skillscouter.com/get-hired-in-2026/)
