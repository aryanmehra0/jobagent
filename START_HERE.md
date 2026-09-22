# Start applying with Job Agent

For a new installation, use Python 3.11 or newer and run these commands from
the project folder on Windows. The explicit environment path avoids needing
to change PowerShell's activation policy:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe main.py ui
```

Upload your own resume in the dashboard. API keys are optional; without them,
the agent uses deterministic scoring and document preparation. Fresh job searches
still require internet access. Use the same environment's Python for later commands.

Open a terminal in this project and start the dashboard:

```powershell
python main.py ui
```

**To download your results:** click **Jobs & downloads → Download everything
(ZIP)**, right-click the saved ZIP and choose **Extract All**, then open
`index.html`. It shows the review shortlist first and links to each included
resume, cover letter, interview guide, CSV and Excel workbook. You can also use
the individual download links beside each job. This downloads saved results;
run the agent first whenever you need a fresh search.

1. **Confirm your profile.** Upload your resume if needed. Check the extracted
   name, skills, employment dates and achievements. Correct the source resume
   and run intake again if anything was parsed incorrectly. Unchanged, sealed
   profiles are reused on later full runs.
2. **Set your search.** In Settings, choose your roles, cities, countries,
   remote/hybrid/onsite preferences, salary floor and freshness window. Set your
   actual work-authorization preferences; willingness to work remotely does not
   establish authorization to work in another country.
3. **Prepare applications.** Leave Dry run enabled, select Country-specific ATS,
   and click Run all phases. Jobs are fetched, filtered, deduplicated, evaluated,
   tailored, checked, routed and tracked. Missing descriptions remain unscored.
4. **Review the outcome.** Open Jobs & downloads. Check the search timestamp,
   source health and run status. Ready for review means a current validated PDF
   exists; you still need to read the PDF, eligibility notes and employer listing.
5. **Apply.** Open the job link and attach the matching PDF. For a published
   hiring contact, review the unsent email draft and send it yourself. After a
   manual submission, click Mark as applied; Undo applied marker corrects mistakes.
   This marker only records your action and never submits anything.
6. **Optional browser submission.** For supported public forms, disable Dry run
   and run the Apply step only after reviewing the jobs. The UI asks for explicit
   confirmation. Login-required portals and unsupported forms require manual
   application; their links and reasons are recorded.

## Downloads after a run

Interview preparation is now Phase 7. Open a job's **Interview Prep** link for
ten questions, a role briefing and STAR practice prompts. The evidence is copied
from your sealed profile; fill bracketed context/result prompts yourself. Full
runs include this phase. To prepare the current shortlist without another search:

```powershell
python main.py prep --offline
```

Enable **Cover letters** before running Tailoring, or use
`python main.py tailor --mode regional --cover-letter`. This is opt-in; it creates
a separate validated one-page PDF per role. Review it before attaching it.

The **Analytics** inspector tab counts actual applications and observed replies.
Dry runs and unsent drafts never count as applications. **Why this score** in
each job shows the existing reasoning, matches, gaps and any reported gap already
present in your profile. It does not change the fit score or invent skills.

For reply tracking, set `IMAP_HOST`, `IMAP_USER`, `IMAP_APP_PASSWORD` and an explicit
`IMAP_FOLDER` in your local `.env`, then run `python main.py sync-inbox`. This reads
up to 100 pending messages in that folder from the past 14 days. It never marks
messages read or sends mail. Matching requires a known employer domain, company,
role and recorded application; ambiguous messages are ignored. Bodies are not
saved. `INBOX_USE_LLM=1` separately opts into sending ambiguous matched message
text to your configured provider. Without that flag, classification stays local.

`python main.py contacts --limit 5` checks public employer team/about pages for
explicit names and relevant titles. Results appear under **Possible contacts
(unverified)**. These are leads, not referrals or verified email recipients.

`python main.py workday-assist --job-id <id>` opens a visible browser for a
qualified Workday job with a validated resume. It fills supported profile fields,
leaves demographic disclosures alone and stops before final submission. Account
access and unknown required answers remain yours to complete. Workday live
submission remains disabled; public-page checks could not validate the complete
application flow. iCIMS, Taleo and SuccessFactors have no new dedicated adapter.

| File in `data/outputs/` | Use it for |
| --- | --- |
| `jobs_latest.csv` | All matching listings observed in the most recent search, including previously seen jobs |
| `applications_ready.csv` | Current jobs with validated PDFs ready for your review |
| `jobs_master.csv` | Cumulative history, scores, contacts, dates, status and next steps |
| `application_pack.zip` | Portable CSVs, verified PDFs, clickable index and reports |
| `applications_tracker.xlsx` | Application and outreach tracking |
| `run_report.json` | Completed phases, failures, pending work and export warnings |
| `source_coverage.json` | Sources checked, cached responses, failures and filtering counts |
| `evaluation_progress.json` | Scored, rejected, deferred and missing-description counts |

Extract the entire ZIP before opening `index.html`. CSV files cannot embed PDF
attachments, so the pack keeps the CSV paths and PDF files together.

The master CSV updates after every completed phase. The ZIP is rebuilt at the
end, including when the run stops with no matches or an error. If Excel locks a
file, the run reports an export warning; close the file and run:

```powershell
python main.py export --bundle
```

## Daily command-line run

```powershell
python main.py run-pipeline --skip-intake --dry-run --mode regional
```

Dry run is the default for the full pipeline. `--live` explicitly enables
submission and retains the confirmation prompt. The CLI and dashboard use the
same run engine. `--limit 10` can bound scoring/tailoring, but it leaves remaining
jobs pending; the run report records that limit's effect.

Repeat searches refresh the latest shortlist without reapplying to processed
jobs. Pending jobs are carried forward while they remain inside the freshness
window. A sweep with no new processing work retains earlier validated outputs.
Public feeds use short-lived caches to respect their polling limits; the sheet
labels cached data. The last search timestamp is not a guarantee that an employer
still accepts applications. Confirm the live listing before submitting.

## If a step needs attention

- **Source failed:** check the source report, adjust unavailable sources, and retry.
- **No qualifying jobs:** read scores and reasons; refine roles or freshness before
  lowering match thresholds. Do not assume every discovered job fits.
- **Description unavailable:** inspect the employer page. The agent leaves it
  unscored and retries pending descriptions on later searches.
- **Resume validation failed:** inspect the source profile or use Preserve original
  PDF. The pack omits PDFs that do not match the active profile and recorded hash.
- **Manual application required:** use the supplied apply link; it is not a failed
  job opportunity or a confirmed submission.
- **No email found:** use the careers link. Addresses are never guessed, and a
  published address is not a guarantee of deliverability.

For setup problems run `python main.py doctor`. For readiness checks run
`python main.py production-check`. This is a local personal application; its
dashboard must remain on localhost. No tool can guarantee every internet vacancy,
perfect employer matching or an interview.
