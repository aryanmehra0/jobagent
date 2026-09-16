# Autonomous AI Job Search & Application Agent

A local, six-stage job hunting pipeline. Each stage reads the previous stage's
artifact from `data/outputs/` and writes its own, so stages can be run
individually, re-run, or chained end to end.

| # | Stage | What it does | Output |
| --- | --- | --- | --- |
| 1 | **Intake** | Converts a PDF resume into a strict, cryptographically sealed `profile.json` | `data/profiles/profile.json` |
| 2 | **Sourcing** | Scrapes job boards via `python-jobspy` plus direct Greenhouse/Lever/Ashby feeds, with proxy rotation and SQLite deduplication | `scraped_jobs.json` |
| 3 | **Evaluation** | Two-tier matching: dense-embedding pre-filter, then an LLM judge scoring 1.0-10.0 | `evaluated_jobs.json`, `qualified_jobs.json` |
| 4 | **Tailoring** | Rewrites bullets for each target role and compiles a single-column ATS PDF with Typst (~100 ms) | `tailored_resumes/*.pdf` |
| 5 | **Auto-apply** | Navigates portals with Playwright using DOM-only perception, with a step ceiling and a human-in-the-loop pause | `application_results.json` |
| 6 | **Tracking** | Logs outcomes to a styled Excel workbook with a personalized cold outreach email per role | `applications_tracker.xlsx` |

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
```

**No API key is required.** Every LLM-backed stage has a deterministic fallback,
and the pipeline runs end to end without one. Keys improve extraction quality,
scoring nuance, and the wording of tailored bullets and outreach emails.

`ANONYMIZED_TELEMETRY=false` is enforced in-process so that no dependency reports
applicant data externally.

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
- **Run one phase** from its own node, or **run all six** from the top bar.
- **Watch progress live.** Nodes turn blue and their incoming connector animates
  while a phase runs; the Live log tab streams the same output the CLI prints.
- **Inspect results.** Clicking a node shows its metrics, top matches, the files
  it produced, and what the anti-hallucination gate did.

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
tells you which constraint was responsible. Already-seen jobs are skipped via
`delta_store.db`.

To poll specific companies' ATS boards directly, add them to `searches.yaml`:

```yaml
ats_companies:
  greenhouse: [stripe, figma, databricks]
  lever: [ramp]
  ashby: [linear]
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

### Stage 5 — Auto-apply

```powershell
python main.py apply --dry-run     # Rehearse; no browser, nothing submitted
python main.py apply               # Live; asks for confirmation first
python main.py apply --yes         # Skip the confirmation prompt
```

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

### Everything at once

```powershell
python main.py run-pipeline --dry-run
python main.py run-pipeline --skip-intake --limit 10
```

Runs stages 1-6 in order, stopping cleanly if a stage produces nothing.

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
| `is_remote` | When true, non-remote postings are dropped |
| `hours_old` | Freshness window, 1 to 8760 hours |
| `job_boards` | `linkedin`, `indeed`, `glassdoor`, `zip_recruiter`, `google`, `bayt`, `naukri`, `bdjobs` |
| `country_indeed` | Country for the Indeed and Glassdoor backends |
| `min_salary` | Compared against the top of a posting's band; postings with no band are kept |
| `max_results_per_board` | Per board, per search term |
| `ats_companies` | Direct ATS boards to poll |
| `proxy_url` | Overrides `RESIDENTIAL_PROXY_URL` |

---

## Tests

```powershell
python -m pytest -v
```

189 tests across the six stages and the console. The ones worth knowing about:

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
- **Auto-apply targets standard HTML forms.** Portals built entirely on custom
  widgets, and LinkedIn Easy Apply, frequently need manual completion; those jobs
  are routed to stage 6 with the reason recorded.
- **Years of experience is computed from your role dates**, with overlapping
  roles merged. If that disagrees with the total stated on your resume, both
  `intake` and `verify` say so rather than silently picking one.
