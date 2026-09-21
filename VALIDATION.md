# Validation and remaining live checks

Validation performed on Windows with Python 3.14 on 2026-09-16.

## Verified locally

- Existing suite: 196 tests passed with normal filesystem and network access.
- Final suite after changes: 199 passed, 1 opt-in browser test skipped (50.41s).
  The browser test was enabled and run separately: 1 passed.
- After the resilience fixes: 227 tests, all passing. The 3 opt-in browser
  tests were enabled and run separately against real headless Chromium: 3 passed.
- Six-stage integration: generated sample PDF -> sealed profile -> fixture job
  sourcing and deduplication -> TF-IDF and deterministic scoring -> searchable
  Typst PDF -> dry-run application result -> Excel tracker. No external LLM or
  employer service is needed for this integration test.
- Repeating an empty batch replaces the evaluation, tailoring, and application
  artifacts when each respective stage is invoked.
- Tracking submitted jobs preserves their applied status and does not queue them
  for fallback outreach. Repeated tracking updates the same spreadsheet row.
- Application results survive a browser cleanup exception.
- Actual headless Chromium fills name and email, uploads a resume, submits an
  intercepted test form once, and recognizes its confirmation. Browser requests
  in this test are intercepted; no real application is submitted.
- `python main.py doctor`: required modules and Chromium installed. Groq is the
  active provider (`openai/gpt-oss-120b`) with `LLM_STRICT=true`, so an LLM stage
  that fails stops the run rather than silently degrading to heuristics.

## Fixes from this review

- Clear a stage's stale output on empty evaluation, tailoring, and apply runs.
- Persist application outcomes in a finally block using atomic file replacement.
- Preserve applied/dry-run lifecycle state when tracking.
- Exclude confirmed submissions from default outreach collection.
- Avoid repeated model loading attempts after the embedding fallback is selected.
- Remove an embedding-backend-dependent assumption from the evaluation test.
- Ignore uploaded DOCX and text resumes in Git alongside candidate PDFs.

## What these checks do not establish

Live job-board availability, third-party portal compatibility, authentication,
CAPTCHA handling, and paid LLM output quality require separate integration checks.
The fixture sourcing test verifies the pipeline handoff, not live scraping.
A successful local browser form does not establish support for every employer's
multi-page or custom application portal.

The profile seal covers locked facts; it is not proof that every profile field
or generated sentence is factually correct. Metric checks cannot guarantee the
truth of nonnumeric LLM claims. Review extracted candidate details and tailored
documents before enabling real submissions.

## Production hardening since the first review

Several items this document originally listed as open are now implemented:

- **Cross-process locking.** `job_agent.runtime.pipeline_lock` serialises every
  artifact-writing stage across CLI processes and dashboard threads using an
  OS file lock on `data/outputs/.pipeline.lock`. A second writer fails fast
  with a clear message instead of interleaving output.
- **Stale downstream artifacts.** `invalidate_after` archives a stage's
  downstream reports into `data/outputs/history/<timestamp>_<stage>/` whenever
  that stage produces a new batch, so a later stage cannot consume results
  built from a superseded upstream.
- **Resumable LLM stages.** Evaluation and tailoring checkpoint after every job.
  An interrupted batch — typically a Groq rate limit in strict mode — resumes on
  the next run and sends only unfinished jobs to the model. A checkpoint is
  reused only for the identical profile content and judge, and a reused
  tailored PDF is re-hashed and rebuilt if its bytes changed.
- **Groq intake repairs.** Source-verifiable fields (dates, bullets, locked
  facts) are reconciled from the resume text *before* the draft is validated.
  A date the model omitted no longer fails intake when the resume states it,
  and never costs a second model call. A date absent from the source is still
  reported as an error rather than invented.

These are covered by `tests/test_runtime_safety.py` and
`tests/test_live_resilience.py`. The latter reproduces the specific failures
recorded under `data/outputs/live_validation/`.

## Apply routing and contact discovery

- **Submit detection.** "Apply" was matched as a submit button, so on an ATS
  listing page the agent clicked "Apply for this job" and recorded the attempt as
  finished before any form was shown. Submit now requires a form on the page (a
  file input, or email plus name), and "Apply" is no longer a submit pattern.
  This was checked read-only against live Lever, Greenhouse and Ashby forms: each
  form page's "Submit application" button was found, and the Lever listing page
  was correctly not treated as a form.
- **Real form URLs.** The Greenhouse embed form, Lever `/apply` and Ashby
  `/application` are derived from the listing. Indeed's `job_url_direct`, which
  JobSpy returned but sourcing threw away, is kept as the apply URL.
- **Manual routing.** Login-walled boards and account-based systems are skipped
  *before* the application is claimed. Of 20 live Bengaluru results, the 8
  LinkedIn jobs routed to manual apply. Indeed employer pages routed to the
  employer site, except Workday and Amazon Jobs, which routed to manual apply.
- **Search boxes.** The form filler no longer types into site search fields.
- **Contacts.** Only published addresses are kept, and each records its source
  and source page. Live, 2 of 20 jobs had an address without Hunter.io (one from
  a job post, one from a company site). A department mailbox
  (accommodations@) found in that run is now filtered out.
- **Jobs CSV.** `jobs_master.csv` is upserted by job ID after every phase.

Covered by `tests/test_contacts.py` and `tests/test_contact_discovery.py`.

## Preferences, freshness, de-duplication and outreach

- **Candidate preferences.** Country, work authorization, sponsorship,
  remote-anywhere and salary range are stored in `preferences.json`. They are
  applied to the sealed profile and re-applied after every intake. Locked facts
  are verified before re-sealing.
- **Sponsorship by location.** Sponsorship is answered from the job's location:
  "No" for India and for remote jobs, "Yes" for on-site jobs abroad. "Authorized
  to work" is answered "Yes" only for a listed country; any other country is left
  for the candidate. Screening answers never assert more than the profile states.
- **Location parsing.** "San Francisco, CA" and "Indianapolis, IN" end in US state
  codes, not country codes. A trailing code counts as a country only in
  "City, ST, CC" form.
- **Strict window.** Dated postings older than `hours_old` are dropped, and so
  are undated ATS-feed postings. Greenhouse uses `first_published`, because
  `updated_at` changes on every edit.
- **No repeats.** A company-plus-title fingerprint de-duplicates within a sweep
  and against every earlier sweep. Existing `delta_store.db` files are migrated
  in place.
- **Outreach ledger.** Drafts are keyed by recipient and role, with a 14-day hold
  per recipient. Each draft is an `.eml` with the tailored resume attached. The
  agent never sends email.
- **Dashboard.** It labels the results of a previous run and offers "Start
  fresh", which archives those results but keeps the de-duplication state.

Covered by `tests/test_preferences_dedupe_outreach.py`.

## Findings from the first live end-to-end run (Bengaluru/NCR, 48h window)

- **Off-target titles.** 826 listings came back, and 379 were new after
  filtering. Of those, 154 were off-target titles (Business Analyst, Credit Risk
  Associate). A title filter now requires every core word of a target role, with
  AI/ML spellings folded together. That leaves 181 of 379, and the credit-risk
  role that scored 7.5 is gone.
- **Jobs never evaluated.** Jobs that evaluation did not reach were lost once
  marked seen. They now carry over to the next sweep while still inside the time
  window.
- **Wrong mailboxes.** reportfraud@, disabilityrecruitment@, alumni.network@,
  accomodations@ and candidatehelpdesk@ were collected. They are now rejected.
  Company websites contribute only hiring and general mailboxes, never named
  individuals.
- **Groq free tier.** The limit is 200,000 tokens per day per model, shared by
  all keys. It is now reported plainly, and `GROQ_FALLBACK_MODEL` continues the
  run on a second model. Per-minute cooldowns of up to 65s are waited out, and
  malformed-JSON 400 responses are resampled.
- **Tailoring.** Resumes differed only in bullet order. Skills named in the
  posting and the most relevant projects now come first. Nothing is reworded.
- **Result.** 5 roles qualified (fit 7.2 to 8.2), each with its own tailored PDF.
  Two had published hiring emails and got ready-to-send `.eml` drafts with the
  resume attached. A re-run made no new AI calls and created no duplicate drafts.

## Faithful resume tailoring

Resumes generated from the extracted profile lost the headline, all three
profile links, the relocation line, role descriptors, written dates, coursework,
both publications, project roles and skill groups, and ran to 2 pages instead of 1.
One bullet was split at a line wrap and its halves separated. Tailoring now
reorders blocks inside the candidate's own PDF instead.

- **Duplicated text.** Copying clipped regions of a page made pypdf read the whole
  page once per region, so every word appeared twice. Each block is now copied
  from a page where all other text is deleted, not just hidden.
- **Unreadable glyphs.** Re-typing text with the PDF's embedded fonts drew empty
  boxes, because the subset fonts have no character map. The original glyphs
  are never re-typed.
- **Reading order.** Drawing moved blocks after the rest of the page put a role's
  bullets after SKILLS in the text an applicant tracking system reads. Pages are
  now drawn as strips from top to bottom. Two reading-order checks guard this,
  and a test reproduces the defect.
- **Sidebars.** Text in a second column was absorbed into a bullet as if it were
  a wrapped line. Wrapped lines must now align with the bullet text, and
  right-half text blocks any move beside it.
- **Result.** 29 resumes were rebuilt (including 13 whose files had been deleted),
  and 29 passed all 10 checks. The rebuild without moves is pixel-identical to the
  original.

Covered by `tests/test_faithful_tailoring.py`.

## Still open

- Artifact paths are shared rather than namespaced per run. History archives
  preserve superseded batches, but there is no run ID linking one run's
  artifacts end to end.
- The application output is a batch report. Earlier batches survive only as
  history archives, not as a queryable log.
- Groq rate limits are organisation-wide. The client waits out short cooldowns
  (up to 30s each, 60s total); a longer one stops the run. Re-running resumes
  from the checkpoint rather than starting over.

## Reproduce

```powershell
python main.py doctor
python -m pytest -ra
$env:JOB_AGENT_BROWSER_TESTS='1'
python -m pytest tests/test_browser_integration.py -ra
python main.py ui
```

Use the dashboard to upload the actual candidate resume, verify extraction, and
set search targets. Start with dry-run enabled. Live application submission is a
separate user action and was not performed during this review.
