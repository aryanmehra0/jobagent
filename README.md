# Autonomous AI Job Search & Application Agent

## Job shortlist and portable downloads

Nine roadmap improvements landed on top of the six-stage core; full detail,
verification notes and known limits are in
[Implementation status](ROADMAP_IMPLEMENTATION.md). Summary of what each one
actually is (not what it aspires to be):

| Feature | Command / trigger | Dashboard? |
| --- | --- | --- |
| Daily application pack | `python main.py daily --limit 5` | Same engine as Run all phases |
| Repair current run | `python main.py repair` | Rebuilds outputs; reruns downstream phases if descriptions improve |
| Quality score | `python main.py quality` | Included as `quality_report.json` in the ZIP |
| Interview prep (Phase 7) | `python main.py prep [--job-id] [--offline]` | Yes — its own phase node |
| Cover letters | `--cover-letter` on `tailor`, or the dashboard run toggle | Yes — run-form checkbox |
| Outcome analytics | automatic once jobs are tracked | Yes — Inspector "Analytics" tab |
| Reply tracking (opt-in) | `python main.py sync-inbox --days N --limit N` | No — CLI only, reply state shows up in the CSV/tracker afterwards |
| Warm contact leads | `python main.py contacts --job-id --limit N` | No — CLI only; results appear passively as "Possible contacts" once run |
| Workday assisted apply | `python main.py workday-assist --job-id ID` | No — deliberately interactive/blocking, doesn't fit the async dashboard runner |
| Hosted per-user API keys | `python main.py hosted-key --user X` / `--revoke KEY_ID` | N/A — hosted control-plane, not the local dashboard |
| CI | `.github/workflows/ci.yml` | — |
| Score explanation | automatic in Phase 3 evaluation output | Yes — already part of the existing job detail view |

Reply tracking, warm contacts and Workday assist are real, tested capabilities,
but they're CLI-first: you run the command, and the dashboard only reflects
what it produced. Hosted auth is a per-user API-key/token layer over the
existing hosted scaffold (isolated request access, not a running multi-tenant
worker yet) — see [Implementation status](ROADMAP_IMPLEMENTATION.md) and
[`DEPLOYMENT.md`](DEPLOYMENT.md) for exactly what's still scaffolding.

[![CI](https://github.com/aryanmehra0/jobagent/actions/workflows/ci.yml/badge.svg)](https://github.com/aryanmehra0/jobagent/actions/workflows/ci.yml)

For the complete daily workflow, read [Start here](START_HERE.md). Full runs now
share one CLI/dashboard engine and automatically publish `jobs_latest.csv`,
`applications_ready.csv`, the master CSV, and the PDF pack. `run_report.json`
records warnings, failures and incomplete scoring. The full CLI pipeline defaults
to dry run; `--live` explicitly enables real submissions.

Run `python main.py ui` and open **Jobs & downloads**. Search by role, company
or location; filter by remote/hybrid/onsite work and hiring contacts. Current
results are shown first; historical scores are labelled because they may refer
to an earlier profile.

Choose **Country-specific ATS** beside Run to generate English PDFs using the
job location: Letter for US/Canada, A4 elsewhere, and regional section headings.
Ambiguous or worldwide locations use an international format. Original
achievements, qualifications and dates stay intact. These are presentation
presets, not translation or a guarantee of every employer's requirements.
**Preserve original PDF** remains available.

```powershell
python main.py tailor --mode regional
python main.py tailor --mode regional --country UK --job-id <id>
python main.py export
python main.py export --bundle
```

**Download everything (ZIP)** produces `data/outputs/application_pack.zip` containing:

- `jobs.csv`: jobs, scores, contact provenance, skill gaps, region, eligibility
  notes and relative resume paths.
- `resumes/`: current validated PDFs matching the profile and manifest hashes.
- `index.html`: a clickable shortlist; extract the entire ZIP before opening.
- `manifest.json`: PDF checksums and reasons unverified or older PDFs were omitted.
- `source_coverage.json`, when available: the most recent sourcing report.

CSV cannot embed PDF attachments. The ZIP keeps files and relative paths
together across computers. The standalone CSV also retains local Excel links.
Downloading does not send applications or emails.

Sourcing includes JobSpy, configured Greenhouse/Lever/Ashby boards, Remotive,
Arbeitnow and [Jobicy's public feed](https://jobicy.com/jobs-rss-feed).
Responses are cached (Remotive: six hours; other public feeds: one hour).
[Remotive requires attribution and limited polling](https://github.com/remotive-com/remote-jobs-api);
source names and original URLs are retained. The coverage report records blocked
boards, public-feed failures, cached requests, filtering and duplicates.
Coverage is limited to configured, accessible sources; a cached listing does
not prove an employer still accepts applications.

Remote willingness does not override a posting's country restriction. Named
foreign-only locations are excluded when residence is known. Unclear region
or timezone requirements remain marked for review. These are location checks,
not assertions about legal work authorization.

Contacts are published or provider-supplied, never guessed. The sheet identifies
the source page and marks deliverability as unchecked. Missing HR emails remain
blank with a clear status. Regional layouts use clear headings and omit added
demographic details, following general principles in the
[National Careers Service CV guide](https://nationalcareers.service.gov.uk/careers-advice/cv-sections).

---

A local, seven-stage job hunting pipeline. Each stage reads the previous stage's
artifact from `data/outputs/` and writes its own, so stages can be run
individually, re-run, or chained end to end.

| # | Stage | What it does | Output |
| --- | --- | --- | --- |
| 1 | **Intake** | Converts a PDF resume into a strict, cryptographically sealed `profile.json` | `data/profiles/profile.json` |
| 2 | **Sourcing** | Scrapes job boards via `python-jobspy` plus direct Greenhouse/Lever/Ashby feeds, with proxy rotation and SQLite deduplication | `scraped_jobs.json` |
| 3 | **Evaluation** | Two-tier matching: dense-embedding pre-filter, then an LLM judge scoring 1.0-10.0 | `evaluated_jobs.json`, `qualified_jobs.json` |
| 4 | **Tailoring** | Rewrites bullets for each target role, optionally drafts a cover letter, compiles a single-column ATS PDF with Typst (~100 ms) | `tailored_resumes/*.pdf`, `cover_letters/*.pdf` |
| 5 | **Auto-apply** | Navigates portals with Playwright using DOM-only perception, with a step ceiling and a human-in-the-loop pause | `application_results.json` |
| 6 | **Tracking** | Logs outcomes to a styled Excel workbook with a personalized cold outreach email per role | `applications_tracker.xlsx` |
| 7 | **Interview prep** | Drafts likely technical/behavioral/company-fit questions and evidence-backed STAR answers per qualified job | `interview_prep/<job_id>.md` |

Two more stages exist but sit outside this numbered chain because they're
opt-in and read from outcomes, not toward them: **reply tracking**
(`sync-inbox`, reads your inbox to detect rejections/interviews/offers) and
**warm contact discovery** (`contacts`, reads employer team pages for
possible referral leads). Both are covered under Commands below.

---

## The accuracy guarantee

Everything this agent produces goes to a real employer under your name, so one
rule governs the whole pipeline: **no stage may assert a fact about you that is
not in your resume.**

Three gates enforce it, and each is covered by tests in
`tests/test_anti_hallucination.py`:

1. **Intake.** The offline extractor copies only what the document contains. A
   field it cannot find is reported as missing, never filled with a placeholder.
   When an LLM does the extraction instead, every metric it returns is checked
   against the resume text and dropped if it is not there.
2. **Tailoring.** Rewritten bullets are compared against the sealed profile. A
   metric the rewrite invented is removed and the original bullet restored; a
   locked metric the rewrite dropped is put back. Both are recorded in the
   manifest so any PDF can be audited afterwards.
3. **Form filling.** A field with no backing fact is left blank and reported,
   rather than filled with a plausible guess. Work-authorization and salary
   answers come from your profile or are skipped.

The profile is sealed with a SHA-256 hash over its locked facts.
`python main.py verify` detects any edit, and stages 4 and 5 refuse to run
against a profile whose seal does not verify.

---

## Setup

Requires Python 3.10+.

```powershell
pip install -r requirements.txt
python -m playwright install chromium   # Only needed for stage 5
copy .env.example .env                  # Then fill in what you have
python main.py doctor                   # Confirms what is installed and configured
python main.py production-check         # Release/readiness checklist
```

**No API key is required.** Every LLM-backed stage has a deterministic fallback,
and the pipeline runs end to end without one. Keys improve extraction quality,
scoring nuance, and the wording of tailored bullets and outreach emails.

`ANONYMIZED_TELEMETRY=false` is enforced in-process so that no dependency reports
applicant data externally.

---

## A-to-Z quick start for a real user

1. Clone the repository and open a terminal in the project directory.
2. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

3. Create `.env`:

   ```powershell
   copy .env.example .env
   ```

4. Optional, but recommended for better scoring and writing:

   ```env
   DEFAULT_LLM_PROVIDER=groq
   GROQ_API_KEYS=gsk_first,gsk_second,gsk_third
   ```

5. Check the machine:

   ```powershell
   python main.py doctor --live
   ```

6. Check your resume before importing:

   ```powershell
   python main.py check "C:\Users\you\Desktop\resume.pdf"
   ```

7. Import and seal your profile:

   ```powershell
   python main.py intake --resume "C:\Users\you\Desktop\resume.pdf"
   python main.py verify
   ```

8. Save eligibility and salary preferences:

   ```powershell
   python main.py preferences --country India --authorized India --sponsorship --remote-worldwide --salary 1000000 --salary-max 1400000 --currency INR
   ```

9. Edit targets in the dashboard or in `config/searches.yaml`.
10. Run the production checklist:

    ```powershell
    python main.py production-check
    ```

11. Run a safe end-to-end rehearsal:

    ```powershell
    python main.py run-pipeline --skip-intake --dry-run --limit 10
    ```

12. Open the dashboard for visual control:

    ```powershell
    python main.py ui
    ```

    In **Settings > AI provider**, choose Groq, OpenAI, Anthropic,
    OpenAI-compatible, or deterministic fallback. Paste the key there; the
    dashboard saves it to your local `.env` and never shows the secret back.

13. Review generated files in `data/outputs/`:

    - `jobs_master.csv`
    - `applications_tracker.xlsx`
    - `tailored_resumes/*.pdf`
    - `outreach/*.eml`

14. Only after reviewing the dry run, run live application mode:

    ```powershell
    python main.py apply --limit 3
    ```

The agent never sends emails. It creates unsent `.eml` drafts and records them
in the no-repeat ledger. `python main.py status` now reports how many outreach
drafts exist and how many unique inboxes have been drafted to.

---

## Deployment and hosting

For a personal production run, use the local dashboard or Docker CLI flow:

```powershell
docker compose build
docker compose run --rm job-agent
docker compose run --rm job-agent python main.py run-pipeline --skip-intake --dry-run --limit 10
```

The main compose file starts a persistent Postgres service and passes
`DATABASE_URL` to the app. Local single-user artifacts still live in `data/`, and
the hosted queue uses Postgres when `DATABASE_URL` is set.

Check the database from the container:

```powershell
docker compose run --rm job-agent python main.py db-check
```

Open Adminer at `http://127.0.0.1:8081` and log in with system `PostgreSQL`,
server `postgres`, user `job_agent`, password `job_agent_dev_password`, database
`job_agent`. If Adminer reports `could not translate host name "db"`, change the Server field to `postgres`.

The included dashboard is intentionally local-only. It can launch browser
automation and submit real applications, so it binds to loopback and must not be
put directly behind a public domain. To reach it from your other devices
without exposing it publicly, set `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` in
`.env` and put it behind a private tunnel you control (Tailscale, Cloudflare
Tunnel) — see [`DEPLOYMENT.md`](DEPLOYMENT.md#opening-the-dashboard-from-your-other-devices-private-not-public)
for the exact steps. Once that's set up once, `.\scripts\start_dashboard.ps1`
starts it and prints your tailnet URL in one step.

For a safe hosted smoke test, run the separate token-protected control plane:

```powershell
docker compose -f docker-compose.hosted.yml up --build
```

It exposes `/health`, `/ready`, and a queued `POST /runs` API on port 8080. The
reference worker completes queued runs in validation mode, which proves the API
and queue are wired without sharing one browser profile across users.

For an open-source hosted product with many users, use this repository as the
worker engine and put a multi-user web layer in front of it: authenticated web
app, per-user storage, a queue, isolated browser workers, managed secrets, and a
database such as Postgres instead of shared local SQLite files. See
[`DEPLOYMENT.md`](DEPLOYMENT.md) for the exact hosting model and scale checklist.

The local repo still keeps single-user artifacts in `data/`: `delta_store.db`
tracks seen jobs, lifecycle status, application attempts and outreach. The hosted
queue uses Postgres when `DATABASE_URL` is set and falls back to
`hosted_queue.db` without it.

---

## What your resume needs (A to Z)

Run this first — it tells you exactly what the agent could read from your file,
and what to change for anything it could not:

```powershell
python main.py check "C:\Users\you\Desktop\my_resume.pdf"
```

It writes nothing. You get a readiness score, a table of what was extracted, and
a specific fix for every gap.

### 1. File format

| Format | Support |
| --- | --- |
| `.pdf` | **Preferred.** Text-based only — a scan or photo has no text to read |
| `.docx` | Fully supported, including table-based templates |
| `.txt` / `.md` | Supported |
| `.doc` | Not readable. Open it and save as `.docx` |
| Scanned / image PDF | Not readable. Run OCR, or re-export from the original |

### 2. Required — intake fails without these

| Field | How to write it |
| --- | --- |
| **Name** | On its own line at the very top, above everything else. ALL CAPS is fine |
| **Email** | Anywhere, as plain text: `you@example.com` |
| **Work experience** | Each role needs a **date range** — that is what marks where a role begins |
| **Skills** | A `SKILLS` section. Job matching depends on it |

### 3. Strongly recommended

| Field | How to write it | What breaks without it |
| --- | --- | --- |
| **Phone** | Header, with country code: `+91 98450 11234` | Application form fields are left blank |
| **Location** | `City, Country`, `City, ST`, or `Based in City` | Remote/onsite matching and form fields |
| **Numbers in bullets** | `cut latency by 45%`, `15,000+ users` | These become locked verified facts; without them tailoring has nothing to protect |
| **Summary** | 2-3 sentences under a `SUMMARY` heading | Used verbatim when tailoring |
| **LinkedIn / GitHub** | Write the address as **text** (`linkedin.com/in/you`), not as a hyperlink on the word "LinkedIn" | Only visible text is read, not link targets |

### 4. Section headings it recognises

Any of these, in capitals or Title Case. Compound headings such as
`EDUCATION & PUBLICATIONS` work too.

- **Summary** — summary, professional summary, profile, objective, about, overview
- **Experience** — experience, work experience, professional experience, employment, work history
- **Education** — education, academic background, academics
- **Skills** — skills, technical skills, core competencies, technologies, tech stack
- **Projects** — projects, key projects, personal projects, portfolio
- **Certifications** — certifications, certificates, licenses
- **Contact** — contact, contact details, personal details

### 5. Role formats it understands

All three of the layouts templates actually produce:

```
Acme Corp — Senior Engineer (2021 - Present)           <- inline
```

```
Acme Corp                         Jan 2021 - Present   <- right-aligned,
Senior Engineer                                           title on the next line
```

```
Senior Engineer                                        <- stacked
Acme Corp
March 2021 - Present
```

Dates may be `2021`, `2021 - 2024`, `Mar 2021 - Present`, or a single year.
`Present`, `Current` and `Now` all mean ongoing.

Start each achievement with a bullet character (`•`, `-`, `*`). A bullet that
wraps onto a second line is rejoined automatically.

### 6. Numbers it captures

Percentages (`42%`, `92%+`), currency in `$ € £ ₹ ¥`, Western magnitudes
(`2.5M`, `340k`) **and Indian ones** (`45 crore`, `1.5 lakh`), multipliers (`4x`),
counts with thousands separators (`15,000+ users`), and plain counts
(`15 bank partners`, `2 manuscripts`).

Version numbers and model names (`PostgreSQL 17`, `1D CNN`) are deliberately
ignored so they are never sealed as achievements.

### 7. Layout

Single-column is most reliable, here and with most employer ATS systems.
Two-column and sidebar templates are read column by column and do work, but
prefer single-column if you have the choice.

### 8. What it will never do

It will not invent a fact. A field it cannot find is reported as missing, not
filled with a plausible-looking value. That is why `check` tells you to fix your
document rather than quietly guessing on your behalf.

---

## The visual flow console

```powershell
python main.py ui
```

Opens a local dashboard at `http://127.0.0.1:8765` showing the whole pipeline as a
node graph, in the style of n8n. Each phase is a node that reports its own status,
and the connectors between them carry the counts handed downstream, so you can see
exactly where the funnel narrows.

### Old results and "Start fresh"

When you open the dashboard it shows what is on disk: the results of your
**previous** run. A blue notice says when that run happened. Press **Run all
phases** to search again, or **Start fresh** first to clear the view.

Start fresh moves the previous results to `data/outputs/history/`. It keeps three
things, because they are what stops a new run from repeating itself:

- the seen-jobs store
- the outreach log
- `jobs_master.csv`

### Candidate preferences

Application forms ask about things a resume rarely says. Set them once, either in
**Settings > Candidate preferences** or from the command line:

```powershell
python main.py preferences --country India --authorized India --sponsorship --remote-worldwide `
  --salary 1000000 --salary-max 1400000 --currency INR
```

They are stored in `data/profiles/preferences.json` and applied again whenever you
upload a new resume. Screening answers then depend on where the job is:

| Job | Visa sponsorship? | Authorized to work? |
| --- | --- | --- |
| In India | No | Yes |
| Remote, for an employer anywhere | No | Yes |
| On-site in another country | Yes | Left for you to answer |

"Expected CTC in LPA" is answered as `14`. A number field gets `1400000`, and a
text field gets `INR 10,00,000 - 14,00,000 per year (10-14 LPA)`. "Current CTC" is
never filled in, because you have not stated it.

### First run: it asks for your resume

The repository ships with a demo resume for a fictional candidate, "Alex Rivera".
Running the pipeline on it would tailor resumes and write outreach emails under
that name, so the console will not let that happen by accident:

- **A setup wizard opens on its own** whenever there is no profile, or the profile
  still came from the bundled sample. Three steps: choose your resume, confirm
  what was extracted from it, set your target roles.
- **Uploading parses immediately** — drop your PDF and the wizard runs phase 1,
  streams the log, then shows you the name, roles, years and locked facts it read
  so you can confirm it is actually you before anything else runs.
- **The demo resume is tagged `DEMO`** in the file list, and can be deleted from
  there.
- **An amber banner stays up** for as long as the loaded profile is demo data.
- **Running any phase on demo data asks first**, naming the candidate it would
  apply as.

Prefer the terminal? `python main.py intake --resume "C:\Users\you\Desktop\my_resume.pdf"`
does the same thing, and the console picks it up on its next refresh.

What you can do from it:

- **Upload a resume** by dropping a PDF onto the Settings tab or into the wizard.
- **Edit the search parameters** without hand-editing YAML; invalid values are
  rejected with the offending field named.
- **Run one phase** from its own node, or **run all seven** from the top bar.
- **Watch progress live.** Nodes turn blue and their incoming connector animates
  while a phase runs; the Live log tab streams the same output the CLI prints.
- **Inspect results.** Clicking a node shows its metrics, top matches, the files
  it produced, and what the anti-hallucination gate did.
- **Read the funnel in Analytics.** The Inspector's Analytics tab shows
  sourced → qualified → tailored → applied → replied → interview → offer, with
  the conversion rate between each stage, response rate broken down by fit
  score, job source and role, and median time to first reply. It only counts a
  real submission as "applied" — dry runs and unsent drafts never inflate the
  numbers — and reply-stage data is empty until you've run `sync-inbox`.

The console drives the same code the subcommands do, so the two are
interchangeable: run a phase in the terminal and the dashboard reflects it on its
next refresh, because state is read from the artifacts on disk rather than held in
memory.

### Safety

The console can submit real job applications, so it is deliberately locked down:

- It binds to loopback only and refuses to start on a network address.
- Every mutating request needs a per-session token embedded in the page, which a
  cross-origin page cannot read.
- `Host` and `Origin` are checked, closing the DNS-rebinding path around the
  loopback bind.
- The Dry run toggle is on by default. Turning it off and running the apply phase
  requires typing `APPLY` into a confirmation dialog first.

Use `--port` if 8765 is taken, and `--no-browser` to skip opening a window.

---

## Commands

### Stage 1 — Intake and parameterization

```powershell
python main.py intake --resume data/raw_resumes/your_resume.pdf
python main.py configure     # Target domains, locations, freshness, boards
python main.py verify        # Check the profile's fact seal
```

`intake` reports any gaps it found (missing phone, no location, no salary
expectation). Fill those in by editing `data/profiles/profile.json` directly,
then re-run `verify`.

### Stage 2 — Sourcing

```powershell
python main.py source
python main.py source --no-ats     # Job boards only
```

Prints a funnel showing how many listings each filter removed, so an empty sweep
tells you which constraint was responsible.

- **Time window is strict.** A dated posting older than `hours_old` is dropped.
  Job boards apply the window themselves. Company ATS feeds list every open role,
  so an undated listing there is dropped rather than risk months-old roles.
- **No repeats, across boards and across runs.** A role is identified by company
  plus title, with suffixes such as "Pvt Ltd" and noise words such as "Remote"
  ignored. The same opening on LinkedIn, Indeed and the company site is kept
  once. Of the copies, it keeps the one you can apply to, with every copy's
  emails merged in. A role seen in any earlier run is never new again
  (`delta_store.db`).
- **Explicit work arrangements.** `work_modes` accepts any combination of
  `remote`, `hybrid`, and `onsite`. When `onsite_countries` is set, physical roles
  outside those countries are excluded. Older files that only use `is_remote`
  continue to work.
- **Worldwide remote eligibility.** Set this in Dashboard > Settings > Candidate
  preferences. A listing with an explicit foreign restriction is excluded when
  worldwide remote work is disabled; ambiguous locations stay available for review.
- **Public API coverage.** Remotive and Arbeitnow can be enabled in
  `public_sources`. They use public JSON feeds and fail independently from the
  eight JobSpy sources and direct company ATS feeds.

For every job, sourcing also records:

- **Apply URL**: the real application form, not the board listing. For Indeed jobs
  it is the employer's own page. For Greenhouse, Lever and Ashby it is the public
  form.
- **Contact emails**, each with where it came from:
  - *Job post*: an address printed in the listing, including obfuscated ones
    such as `hr [at] acme [dot] in`.
  - *Company website*: the employer's homepage and its careers, jobs, contact
    and about pages. `robots.txt` is honoured, and only addresses on the
    company's own domain are kept.
  - *Hunter.io*: only when `HUNTER_API_KEY` is set in `.env`. Only role
    mailboxes (careers@, hr@) are requested, never named people.

Addresses are **never guessed**. The agent does not build `firstname.lastname@`
patterns. It also drops mailboxes meant for other teams (accommodations@,
privacy@, press@, sales@). Turn lookups off with `find_contacts: false`.

To poll specific companies' ATS boards directly, add them to `searches.yaml`:

```yaml
ats_companies:
  greenhouse: [stripe, figma, databricks]
  lever: [ramp]
  ashby: [linear]
```

### The jobs sheet (CSV)

```powershell
python main.py export
```

`data/outputs/jobs_master.csv` lists every job the agent has found, one row each.
It is refreshed automatically after every phase, and the dashboard offers it as
a download. Its columns are:

- Title, company, location, work mode, source, date posted and salary
- Fit score and status (found, evaluated, qualified, tailored, then applied,
  failed or skipped)
- **HR / Careers Email**, Email Type, Email Source, Email Found On, Other Emails
- Company Website, Job URL, **Apply URL**, Apply Method, Auto-apply Possible
- Tailored Resume
- **Outreach To**, **Outreach Status**, **Cold Email Subject**, **Cold Email Body**,
  Email Draft File, and Notes

Rows accumulate across sweeps. A job keeps its row, a later phase never blanks
a value an earlier one recorded, and a confirmed "applied" is never overwritten.

### The jobs database

```powershell
python main.py db stats                      # What the database holds
python main.py db jobs --with-email -n 20    # Fetched jobs and their emails
python main.py db jobs --min-score 7 --status qualified
python main.py db sync                       # Rebuild it from the artifacts
python main.py db query "SELECT company, contact_email FROM job_overview WHERE fit_score >= 7"
python main.py db query "SELECT * FROM job_overview" --csv data/outputs/db_export.csv
```

`db query` runs any read-only SQL and refuses anything that would change data,
so it is safe to explore with. To browse the database in a GUI instead, open
`data/outputs/jobs.db` in DB Browser for SQLite (https://sqlitebrowser.org) or
the SQLite Viewer extension in VS Code.

Everything the agent fetches is also written to a database, so you can query it
from a DB client or an admin UI instead of opening a spreadsheet. It uses
Postgres when `DATABASE_URL` is set and SQLite (`data/outputs/jobs.db`)
otherwise, with the same tables either way:

| Table | Holds |
| --- | --- |
| `jobs` | One row per job: title, company, location, salary, source, posting date, description, fit score, stage, apply URL and method, tailored resume |
| `job_evaluations` | The score behind each job: fit, technical and seniority scores, the threshold, the reasoning, and matching and missing skills |
| `job_contacts` | Every published email found for a job, with its kind (hiring, person, general) and the page it came from |
| `job_outreach` | The cold email drafted for a job: recipient, subject, body, draft file and whether it may be sent |
| `job_resumes` | The tailored PDF itself, with its checksum and check result |
| `job_applications` | What the apply phase did: status, channel, apply URL, steps and any error |
| `candidate_profile` | The sealed profile the run used: contact, experience, country, sponsorship, salary expectation, skills, source resume |
| `search_parameters` | The search it ran: roles, locations, on-site countries, freshness window, boards, salary floor |
| `phase_runs` | Run history: each phase, its status, duration and summary, and whether it came from the dashboard or the terminal |
| `job_overview` (view) | One row per job with its best contact email, resume and outreach state, for browsing |

### Opening it in DBeaver

1. **Database → New Database Connection → SQLite → Next.**
2. **Path**: `data\outputs\jobs.db` inside the project folder → **Finish**.
   (With `DATABASE_URL` set, choose PostgreSQL instead and use those details; the
   tables are the same.)
3. Expand **jobs → Schemas → main → Tables** and double-click a table.
4. After a run, right-click the connection → **Refresh** (F5) to see the new rows.

It is refreshed after every phase, alongside the CSV. Some useful queries:

```sql
-- Jobs worth applying to that have a hiring mailbox
SELECT title, company, fit_score, contact_email, apply_url
FROM job_overview WHERE fit_score >= 7 AND contact_kind = 'hiring'
ORDER BY fit_score DESC;

-- Where the contact emails are coming from
SELECT source, COUNT(*) FROM job_contacts GROUP BY source;
```

### Stage 3 — Evaluation

```powershell
python main.py evaluate
python main.py evaluate --threshold 0.05     # Widen the Tier 1 gate
python main.py evaluate --limit 20           # Cap how many reach the LLM judge
```

Tier 1 is a free local embedding pass; Tier 2 is the paid LLM call. Only jobs
that clear Tier 1 reach Tier 2, which is what keeps a large sweep affordable.

### Stage 4 — Tailoring

```powershell
python main.py tailor
python main.py tailor --limit 5              # Top 5 by fit score
python main.py tailor --job-id <id>
```

The summary's "Integrity gate" column reports how many metrics were restored and
how many fabrications were blocked for each resume.

Every compiled resume also gets a `.ats.json` audit beside the PDF. Compilation
fails before application if the PDF is not text-searchable, loses the candidate's
identity, employer names, or source achievements, or grows beyond three pages.

### How resumes are tailored

When your uploaded resume is a PDF, the agent tailors **that file**, not a
rebuilt copy. For each job it only reorders:

- the bullets within each role (roles stay in date order),
- the projects in a projects section,
- the lines of the skills section,

putting what the job description asks for first. Nothing is added, removed or
reworded, and your fonts, colours, links, dates, headline, publications and page
count stay exactly as you made them. No AI call is needed, so rate limits cannot
break this step.

Every tailored PDF is checked against your original before it is used:

| Check | Passes when |
| --- | --- |
| Pages | Same number of pages, same size |
| Words (2 readers) | Every word appears exactly as often as in the original |
| Blocks intact | Every bullet and entry is still one continuous piece |
| Links | Every link kept, with the same text |
| Order | The planned order is what appears on the page |
| Reading order (2 readers) | An applicant tracking system reads sections, roles and bullets in the order you see them |
| Unchanged elsewhere | No pixel changed outside the reordered blocks |

A resume that fails any check is replaced by your exact original. The result
("PASS 10/10 checks - 2 section(s) reordered") is shown in the **Resume Check**
column of the tracker and the CSV, and next to each resume on the dashboard.

```powershell
python main.py tailor                     # Tailor for the qualified jobs
python main.py tailor --rebuild-existing  # Rebuild every resume already listed
```

Your uploaded PDF is always the source when there is one: if a job's reordering
cannot be done safely, that job gets your exact original, never a resume built
from a template. The bundled demo (`sample_resume.pdf`) is never chosen while a
resume of your own is uploaded, and any resume in the output folder that is not
yours is moved to `data/outputs/history/`.

Each tailored resume can be reached from:

- the tracker (`applications_tracker.xlsx`): click the file name;
- the CSV (`jobs_master.csv`): the **Open Resume** column opens it in Excel;
- the database: `job_resumes` holds the PDF itself, and
  `python main.py db resume <job id>` saves it to Downloads, byte for byte,
  ready to attach.

`TAILORING_MODE` in `.env` selects the CLI default: `auto` (your PDF when its
layout can be read, otherwise a resume generated from your profile), `faithful`
(always your PDF), `generated`, or `regional` (country-aware ATS presentation).
The dashboard's Resume selector and CLI `--mode` override this default.

### Stage 5 — Auto-apply

```powershell
python main.py apply --dry-run     # Rehearse; no browser, nothing submitted
python main.py apply               # Live; asks for confirmation first
python main.py apply --yes         # Skip the confirmation prompt
```

Where the browser goes depends on the job:

| Job | What auto-apply does |
| --- | --- |
| Greenhouse, Lever, Ashby | Opens the public application form |
| Indeed job with an employer page | Opens the employer's page and hands off if it finds no form |
| LinkedIn, Indeed-only, Glassdoor, Naukri | **Skipped: manual apply.** These need you signed in |
| Workday, Taleo, SuccessFactors, Amazon Jobs | **Skipped: manual apply.** These need an account created |

Skipped jobs keep their apply link and reason in the CSV and the tracker. They
are never marked as attempted, so a later run can still pick them up. Submit is
clicked only on a page that has a real application form (a resume upload, or
name plus email), never on an "Apply" link.

Uses a persistent Chromium profile (`data/browser_profile/`) so logins survive
between runs. DOM-only perception (`use_vision=false`), a 25-step ceiling, a
single submit attempt per job, and a human-in-the-loop pause on CAPTCHA or MFA.

**Live runs are irreversible.** The confirmation prompt names every employer
before anything is sent; set `REQUIRE_APPLY_CONFIRMATION=false` only for
unattended runs you have deliberately opted into.

### Stage 6 — Tracking

```powershell
python main.py track           # Log the jobs that were not submitted
python main.py track --all     # Log every qualified role
```

Writes `data/outputs/applications_tracker.xlsx` with navy headers, wrapped text,
and priority colour fills driven by the real fit score. Re-running updates each
job's existing row rather than appending a duplicate.

It also drafts a cold email for each role. The agent **never sends email**; it
guarantees you are never handed a second email to the same inbox:

Each draft has a role-and-company-specific subject, the candidate's most relevant
verbatim achievements, matched skills from the posting, a direct call to action,
and the tailored PDF attached. No achievement or email address is invented.

| Outreach Status | Meaning |
| --- | --- |
| Ready to send | First email to this address about this role. `data/outputs/outreach/<job>.eml` opens in Outlook or Thunderbird as an unsent draft with your tailored resume attached |
| Already drafted - do not send again | This address was already given a draft for this role, even if it came from another board or an earlier run. The original text is shown |
| On hold | This address was drafted to about a different role in the last 14 days |
| No email found | The draft is still written, for LinkedIn or the careers page |

The ledger is the `outreach_log` table in `delta_store.db`. Drafts, including
their subject and body, are kept in `outreach_drafts.json`, so re-running
tracking reuses them instead of making another LLM call.

### Stage 7 — Interview prep

```powershell
python main.py prep                    # Every qualified job
python main.py prep --job-id <id>
python main.py prep --limit 5
python main.py prep --offline           # Skip the LLM; template questions and evidence only
```

Refuses to run against a profile whose seal doesn't verify, same as tailoring.
For each job it writes `data/outputs/interview_prep/<job_id>.md` (10 questions —
4 technical from the JD, 3 behavioral, 3 company-fit) plus a `.json` copy. Every
STAR-format answer is built only from bullets and locked facts already present
in the sealed profile — it goes through the same fabrication gate as resume
tailoring (`enforce_metric_integrity`), so an interview answer can't claim
anything your resume doesn't. The "likely panel composition" line is explicitly
labelled a hypothesis, not confirmed company information. Guides are linked
from the dashboard's Interview prep node and included in the ZIP export; a
guide is hidden again if you re-seal the profile with different content, so
you're never handed a guide written for an outdated version of yourself.

### Reply tracking (opt-in, CLI only)

```powershell
python main.py sync-inbox --days 14 --limit 100
```

Off by default — it only runs once you set all four of `IMAP_HOST`,
`IMAP_USER`, `IMAP_APP_PASSWORD`, and `IMAP_FOLDER` in `.env`. Point it at a
**dedicated folder**, not your whole inbox. It opens that folder read-only,
never sends anything, and matches an inbound email to a tracked application
only when the sender's domain is the employer's own (not gmail/outlook/etc.),
the company name matches, and the job title overlaps enough to be unambiguous
— an uncertain match is left alone rather than guessed. Matched emails are
classified as rejection / interview / offer / other, first by keyword rules
and only by an LLM (`INBOX_USE_LLM=1`) when the rules don't recognise the
wording. Already-processed messages are checkpointed in `inbox_events.json`
so re-running never reclassifies or double-counts them, and this is what
feeds the Analytics tab's response-rate numbers below.

### Warm contact leads (opt-in, CLI only)

```powershell
python main.py contacts --job-id <id>
python main.py contacts --limit 5
```

Separate from the hiring-mailbox lookup in sourcing. This looks at an
employer's own `team` / `about` / `leadership` / `people` pages for named
staff with a public title, as a **possible** referral lead — never a claimed
relationship, never a guessed email, and never a social-platform crawl
(LinkedIn and similar are explicitly excluded). A name is kept only if it and
its title both appear as literal visible text on the page; ambiguous cards
with more than one plausible title are dropped rather than guessed. Results
are written to `data/outputs/warm_contacts.json` and show up passively as a
"Possible contacts (unverified)" list next to the job once you've run this —
there's no dashboard button to trigger the crawl itself yet.

### Workday assisted apply

```powershell
python main.py workday-assist --job-id <id>
```

Workday listings are skipped by the normal `apply` phase (account required).
This command is the assisted alternative: it opens a **visible** browser,
requires a validated tailored resume to already exist for that job, fills
every field it can from your profile (skipping anything it can't back with a
fact, exactly like Gate 3), and then **always stops for you** — it never
clicks submit. If a password field, a CAPTCHA, or an unfillable required field
appears, it pauses and hands control to you at the keyboard. Demographic
fields (race, veteran status, disability, gender) are refused outright and
left for you, regardless of channel. Treat it as "get the form 90% filled,
you finish and submit it," not as autonomous Workday submission — that
remains unimplemented by design.

### Hosted API keys

```powershell
python main.py hosted-key --user alice
python main.py hosted-key --revoke <key-id>
```

Only relevant if you're running the separate hosted control-plane scaffold
(see Deployment below), not the local dashboard. Issues a per-user bearer
token shown once; only its SHA-256 hash is ever stored, and lookups use a
timing-safe comparison. `/runs` and `/runs/<id>` on the hosted API are now
scoped to the authenticated user — one user's key cannot see or enumerate
another's runs. This replaces the earlier single shared `HOSTED_API_TOKEN`.
It authenticates and isolates hosted API requests; it does not by itself add
a running multi-user worker — see [`DEPLOYMENT.md`](DEPLOYMENT.md) for what's
still needed before this is a real hosted product.

### Everything at once

```powershell
python main.py run-pipeline --dry-run
python main.py run-pipeline --skip-intake --limit 10
```

Runs stages 1-7 in order, stopping cleanly if a stage produces nothing.

### Diagnostics and reset

```powershell
python main.py status     # State of every stage
python main.py doctor     # Dependencies, keys, browser binary
python main.py reset --delta --outputs
```

---

## Configuration reference

`searches.yaml` is validated on load; an invalid board name or out-of-range value
fails immediately with the offending field named.

| Key | Meaning |
| --- | --- |
| `target_domains` | Job titles to search for |
| `locations` | Cities or `Remote`; every location is searched on every board |
| `work_modes` | Any combination of `remote`, `hybrid`, and `onsite` |
| `is_remote` | Legacy compatibility field; `work_modes` takes precedence |
| `hours_old` | Freshness window, 1 to 8760 hours |
| `job_boards` | `linkedin`, `indeed`, `glassdoor`, `zip_recruiter`, `google`, `bayt`, `naukri`, `bdjobs` |
| `public_sources` | Public JSON feeds: `remotive`, `arbeitnow`, `jobicy` |
| `country_indeed` | Country for the Indeed and Glassdoor backends |
| `min_salary` | Compared against the top of a posting's band; postings with no band are kept |
| `max_results_per_board` | Per board, per search term |
| `ats_companies` | Direct ATS boards to poll |
| `proxy_url` | Overrides `RESIDENTIAL_PROXY_URL` |
| `salary_currency` | Currency of `min_salary`, e.g. `INR` |
| `find_contacts` | Look up published contact emails (default `true`) |
| `onsite_countries` | Optional eligibility countries for onsite and hybrid roles |

---

## Tests

```powershell
python -m pytest -v
```

Tests cover the six stages and the console. The ones worth knowing about:

- `tests/test_pipeline_integration.py` runs all six stages with fixture job listings,
  real PDF parsing and compilation, offline scoring, dry-run application results,
  SQLite deduplication, and Excel output. It also checks empty repeat runs and
  preservation of submitted status.
- `tests/test_browser_integration.py` exercises actual Chromium form filling,
  resume upload and confirmation against an intercepted test page. Enable it with
  `$env:JOB_AGENT_BROWSER_TESTS='1'; python -m pytest tests/test_browser_integration.py`.
  It requires installed Chromium and sends no applications to employers.

- `tests/test_anti_hallucination.py` — the guarantee described above
- `tests/test_validation.py` — schema rejection and normalization rules
- `tests/test_resume_layouts.py` — real-world resume layouts and number formats
- `tests/test_readiness.py` — the `check` report and multi-format reading
- `tests/test_normalize.py` — date, URL, phone, and PDF-artifact handling
- `tests/test_web.py` — flow console state, run lifecycle, demo-data detection,
  and the HTTP guards

---

## Known limitations

- **Glassdoor and ZipRecruiter return HTTP 403 without a residential proxy.**
  LinkedIn and Indeed work directly. Set `RESIDENTIAL_PROXY_URL` to use them.
- **Automated CAPTCHA solving is not implemented.** Every challenge pauses for
  you, even with `CAPSOLVER_API_KEY` set.
- **Auto-apply targets public forms.** LinkedIn, Indeed-hosted applications and
  account-based systems such as Workday are routed to manual apply with the link
  and reason recorded. Portals built on custom widgets may still need finishing
  by hand.
- **Many large employers publish no hiring email.** Expect an address for a
  minority of jobs from job posts and company sites alone; a Hunter.io key raises
  that. LinkedIn gives no company website, so without Hunter.io its jobs only get
  emails printed in the post.
- **Years of experience is computed from your role dates**, with overlapping
  roles merged. If that disagrees with the total stated on your resume, both
  `intake` and `verify` say so rather than silently picking one.
- **`sync-inbox`, `contacts`, and `workday-assist` are CLI-only.** The
  dashboard shows what they produced (reply status, possible-contact list,
  a filled-but-unsubmitted Workday form) once you've run them from a
  terminal; there's no in-dashboard trigger for any of the three yet.
- **`workday-assist` never submits.** It fills the form and stops for you at
  every risky step (password fields, CAPTCHA, required fields it can't back
  with a fact, and the submit button itself). No other account-required ATS
  (Taleo, iCIMS, SuccessFactors) has an assisted path yet — they still route
  to manual apply.
- **Hosted per-user API keys authenticate and isolate requests to the hosted
  control-plane scaffold; they are not a running multi-tenant product.**
  There is still no worker that executes queued per-user jobs in isolation —
  see [`DEPLOYMENT.md`](DEPLOYMENT.md).
- **CI runs only after this workflow is pushed.** The badge above reflects
  `.github/workflows/ci.yml`'s actual run history on GitHub, not a local
  guarantee.
