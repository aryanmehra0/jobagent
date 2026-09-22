# Roadmap implementation and operating limits

Updated 2026-09-22. The local application remains the supported execution path.

| Item | Available now | Verification and limits |
| --- | --- | --- |
| 1. Interview preparation | Phase 7, `prep`, per-job guides, tracker sheet and ZIP links | Ten technical/behavioral/company-fit questions. Exact profile evidence; incomplete STAR context is a prompt for the candidate, never an invented claim. Panel composition is labelled a hypothesis. |
| 2. Reply tracking | `sync-inbox`, rejection/interview/offer/other states, persistent message checkpoints | Fake read-only mailbox tests cover matching and idempotency. No real mailbox was connected. Requires all four IMAP settings and a dedicated folder. No automatic sending. Ambiguous employer/application matches are ignored. |
| 3. Analytics | Inspector Analytics tab and `/api/analytics` | Counts observed outcomes, source/score/format/role response rates and dated response time. Dry runs and drafts are excluded. Unavailable reply data is labelled. |
| 4. Workday assistance | `workday-assist --job-id ID`, existing bounded navigator, profile fields, resume upload and review handoff | Chromium fixtures verify multiple steps, missing required answers, account stops and no final submission. Three public listing checks did not expose usable application forms. Live Workday submission remains disabled. No new dedicated iCIMS, Taleo or SuccessFactors adapter. |
| 5. Cover letters | Optional dashboard toggle and `--cover-letter`; validated one-page PDFs | Exact-source evidence selected through existing integrity machinery. Default runs do not request letters. Generated letters are downloadable individually and in the ZIP. |
| 6. Public contact leads | `contacts --limit 5`, Possible contacts in job rows | Only explicit visible name/title pairs on employer team/about pages. Ambiguous names are dropped. No social-site crawling, guessed emails, claimed relationships or sending. Current shortlist has no discoverable employer sites, so it produced zero leads. |
| 7. CI | Push/PR Python 3.11 workflow, pip cache, README badge, manually enabled Chromium job | Local secret-free suite passed; remote GitHub Actions execution requires these changes to be pushed. No configured linter exists, so none was introduced. |
| 8. Hosted authentication slice | Per-user revocable keys, hashed storage, owner-scoped runs and separate hosted job records | HTTP tests prove cross-user access is denied. SQLite tested locally; equivalent Postgres tables and queries are implemented but no live Postgres authentication test was run. Worker execution remains disabled pending isolated per-user storage/browser workers. |
| 9. Score explanation | Per-job Why this score, matched/missing skills, profile evidence comparison | Reranker now includes all roles and recorded bullets instead of only three roles/six facts. The 6,000-character window applies to the JD, not the profile. Existing saved scores are not silently rewritten. |

## Simple download flow

1. Start `python main.py ui` and run the agent with Dry run enabled.
2. Click **Jobs & downloads → Download everything (ZIP)**.
3. Extract the complete ZIP and open `index.html`.
4. Review each shortlisted job's resume, optional cover letter and interview guide.
5. Apply through its employer link, then record the submission in the dashboard.

The ZIP includes current verified documents, three CSV views, a portable Excel
workbook, run/source reports and a manifest of document hashes. It omits credentials,
browser profiles and old email attachments. Draft email text remains in the CSV;
review it and attach the current PDF yourself. Downloading refreshes the export,
not the underlying job search.

For a fresh terminal run, use `python main.py daily --limit 5`. It runs the same
safe dry-run pipeline as the dashboard, includes cover letters by default, exports
the ZIP and verifies the pack's required files and document hashes before reporting
success.

Use `python main.py quality` after a run to get a numeric readiness score. The
report is saved as `quality_report.json` and is bundled into the ZIP. It currently
scores the profile seal, source freshness, evaluation coverage, ready rows,
document-hash validation, hiring-email coverage and observed outcome tracking.

Tech-stack decision: keep the core local stack (`pydantic`, `PyMuPDF`/
`pdfplumber`, `Playwright`, `Typst`, SQLite/Postgres) because it already supports
the privacy and zero-key requirements. The next optional stack upgrade should be
provider-agnostic structured LLM output through native OpenAI Structured Outputs
or Instructor-style Pydantic retries, and Docling as an optional resume extraction
fallback for difficult PDFs. Both should stay optional so the offline path remains
fast and installable.

## Reproduce validation

Final local verification: **505 tests passed** with provider secrets removed and
Chromium fixtures enabled. After the final download/freshness changes, **89
targeted tests passed**. Browser checks exercised seven download paths without
JavaScript errors. All 15 packaged document hashes matched their manifest.
The saved search contains 42 listings; three currently satisfy the review
shortlist window. Five document sets remain available in the pack. A fresh
search requires running the sourcing phase again.

Recorded results: `data/outputs/validation/roadmap_release_check.json`.

```powershell
python scripts/test_without_keys.py
$env:JOB_AGENT_BROWSER_TESTS = "1"
python scripts/test_without_keys.py
python main.py prep --offline
python main.py export --bundle
```

`test_without_keys.py` removes provider credentials from the child process and
disables `.env` loading. It never changes your real `.env`. Browser fixtures
intercept every page request and submit only to a local test callback.

For CI details, see [the workflow](.github/workflows/ci.yml). Its Python setup and
caching follow [GitHub's Python workflow documentation](https://docs.github.com/en/actions/tutorials/build-and-test-code/python).

Workday read-only findings are documented in [VALIDATION.md](VALIDATION.md); raw
observations are saved under `data/outputs/validation/workday_readonly.json`.
For mailbox setup and daily operation, use [START_HERE.md](START_HERE.md).
For hosted key provisioning and the remaining deployment boundaries, use
[DEPLOYMENT.md](DEPLOYMENT.md).
