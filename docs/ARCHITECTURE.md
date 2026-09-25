# Autonomous AI Job Search & Application Agent — Complete Architecture Reference

> **Version**: 0.3.0 · **Last verified**: 2026-09-25 · **Tests**: 513 passed, 6 opt-in skipped (519 total) · **Phases**: 7 core + 2 opt-in (reply tracking, warm contacts)

This document is the **single source of truth** for any AI agent (Claude, Codex, Gemini, etc.) or developer working on this codebase. It maps every file, class, function, constant, data flow, and design invariant so you can make changes without reading all 78 source files.

> A previous revision of this document named several files that do not exist in this codebase (`prep/pipeline.py`, `hosted/db.py`, `tracking/contacts.py`, `automation/workday.py`). The directory tree and every file reference below were re-verified against the actual filesystem (`find src/job_agent -name "*.py"`) before being written down — do the same before trusting any file path in a doc, including this one, since docs drift and code doesn't lie.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Directory Tree](#2-directory-tree)
3. [Data Flow & Pipeline Architecture](#3-data-flow--pipeline-architecture)
4. [Anti-Hallucination Safety System](#4-anti-hallucination-safety-system)
5. [Configuration & Settings](#5-configuration--settings)
6. [Pydantic Schema Reference](#6-pydantic-schema-reference)
7. [Phase 1 — Intake & Readiness](#7-phase-1--intake--readiness)
8. [Phase 2 — Sourcing & Dedup](#8-phase-2--sourcing--dedup)
9. [Phase 3 — Evaluation & Scoring](#9-phase-3--evaluation--scoring)
10. [Phase 4 — Tailoring & Cover Letters](#10-phase-4--tailoring--cover-letters)
11. [Phase 5 — Auto-Apply & Workday Assist](#11-phase-5--auto-apply--workday-assist)
12. [Phase 6 — Tracking, Warm Contacts & Bundles](#12-phase-6--tracking-warm-contacts--bundles)
13. [Phase 7 — Interview Prep & STAR Briefings](#13-phase-7--interview-prep--star-briefings)
14. [Web UI (Visual Flow Console)](#14-web-ui-visual-flow-console)
15. [CLI Reference (26 Subcommands + `db` group)](#15-cli-reference-26-subcommands--db-group)
16. [Environment Variables](#16-environment-variables)
17. [Test Suite (519 Tests)](#17-test-suite-519-tests)
18. [Key Design Invariants](#18-key-design-invariants)

---

## 1. Project Overview

A **local, seven-stage job hunting and application pipeline** that converts a candidate resume into sealed, fact-locked data, sources live jobs across job boards and direct ATS feeds, scores fit using dense embeddings and an LLM judge, tailors resumes with cryptographic anti-hallucination gates, auto-applies or prepares fallbacks, tracks outcomes with contact discovery and IMAP sync, and synthesizes role-specific interview preparation briefings.

**Core rule**: No stage may assert a fact about the candidate that is not in their resume.

```
Resume PDF → profile.json → scraped_jobs.json → qualified_jobs.json → tailored PDFs → application_results.json → tracker.xlsx & application_pack.zip → interview_prep.json
```

---

## 2. Directory Tree

Verified against `find src/job_agent -name "*.py"` on 2026-09-25 — every path below exists.

```
job agent/
├── main.py                          # Thin entrypoint, adds src/ to sys.path, delegates to cli
├── pyproject.toml                   # Package config, dependencies, pytest configuration
├── requirements.txt                 # Flat dependency list
├── .env.example                     # Environment variables with defaults
├── .gitignore                       # Protects secrets, candidate resumes, outputs
├── README.md                        # Primary user documentation (repo root)
├── docs/                            # Everything else: START_HERE, DEPLOYMENT, VALIDATION,
│                                     # ARCHITECTURE (this file), ROADMAP_IMPLEMENTATION,
│                                     # IMPROVEMENT_ROADMAP
│
├── config/
│   ├── searches.yaml                # Search parameters (domains, boards, locations, ATS targets)
│   └── hosted.example.env           # Hosted control-plane env template
│
├── templates/
│   ├── resume.typ                   # Typst template for single-column ATS resume compilation
│   └── cover_letter.typ             # Typst template for single-page grounded cover letters
│
├── scripts/                         # Operator scripts, not part of the package
│   ├── start_dashboard.ps1          # One command: tailscale serve + launch the dashboard
│   ├── validate_live_pipeline.py    # Isolated live end-to-end check (real network+LLM, demo data)
│   ├── check_live.py, check_sources.py, check_workday_readonly.py, test_without_keys.py
│
├── data/                            # Runtime data (gitignored except the sample resume)
│   ├── raw_resumes/sample_resume.pdf
│   ├── profiles/profile.json        # Sealed candidate profile with SHA-256 fact seal
│   ├── outputs/                     # scraped/evaluated/qualified jobs, tailored_resumes/,
│   │                                 # cover_letters/, interview_prep/, applications_tracker.xlsx,
│   │                                 # application_pack.zip, delta_store.db, jobs.db, hosted_queue.db
│   └── browser_profile/             # Persistent Chromium session (cookies, logins)
│
├── src/job_agent/                   # Core package (78 .py files)
│   ├── __init__.py, __main__.py, cli.py (26 @cli.command entries + a `db` group)
│   ├── workflow.py                  # Shared multi-phase pipeline execution engine
│   ├── generation.py                # Shared LLM-completion + anti-hallucination evidence
│   │                                 # helpers reused by interview prep and cover letters
│   ├── llm.py                       # Provider-agnostic LLM client (groq/openai/anthropic/
│   │                                 # openai_compatible), strict-mode error propagation
│   ├── runtime.py                   # Cross-process pipeline lock, @exclusive_run, cancellation
│   │
│   ├── config/            normalize.py, schema.py, settings.py
│   ├── intake/             Phase 1 — cli.py, heuristic.py, layout.py, parser.py,
│   │                        preferences.py, readiness.py, validator.py
│   ├── sourcing/           Phase 2 — scraper.py, ats_direct.py, delta_store.py, details.py,
│   │                        proxy_manager.py, public_feeds.py, relevance.py
│   ├── evaluation/         Phase 3 — embedder.py, reranker.py, pipeline.py, gaps.py
│   ├── tailoring/          Phase 4 — rewriter.py, compiler.py, cover_letter.py, faithful.py,
│   │                        pipeline.py, regional.py
│   ├── automation/         Phase 5 — agent.py, browser_session.py, form_filler.py, hitl.py,
│   │                        navigator.py, pipeline.py, routing.py (Workday detection/assist
│   │                        logic lives here + in agent.py/form_filler.py/navigator.py, not
│   │                        a separate workday.py file)
│   ├── tracking/           Phase 6 — bundle.py, cold_email.py, export.py, inbox.py, manual.py,
│   │                        outreach.py, pipeline.py, quality.py, records.py, styler.py,
│   │                        supplements.py, tracker.py
│   ├── interview/          Phase 7 — pipeline.py (module is `interview`, not `prep`)
│   ├── contacts/           Warm contact leads (separate top-level package, not under tracking/)
│   │                        — extract.py, finder.py, warm.py
│   ├── storage/            jobs_db.py — the queryable jobs.db/Postgres layer
│   ├── hosted/             Separate control-plane scaffold — api.py, auth.py, queue.py,
│   │                        worker.py (queue+auth storage is queue.py, there is no db.py)
│   └── web/                Flow console — runner.py, server.py, state.py, analytics.py,
│                            static/{index.html,styles.css,app.js}
│
└── tests/                            # 31 test files, 519 tests (513 pass, 6 opt-in browser tests)
```

---

## 3. Data Flow & Pipeline Architecture

```mermaid
flowchart LR
    A["Phase 1: Intake\n(parser.py)"] -->|"profile.json\n(SHA-256 seal)"| B["Phase 3: Evaluation\n(embedder + reranker)"]
    C["Phase 2: Sourcing\n(JobSpy + ATS feeds)"] -->|"scraped_jobs.json"| B
    B -->|"qualified_jobs.json\n(fit ≥ 7.0)"| D["Phase 4: Tailoring\n(rewriter + Typst)"]
    D -->|"manifest.json\n+ tailored PDFs"| E["Phase 5: Auto-Apply\n(Playwright DOM)"]
    E -->|"application_results.json"| F["Phase 6: Tracking\n(Excel + Pack ZIP)"]
    D -->|"cover letters"| F
    B -->|"qualified jobs"| G["Phase 7: Interview Prep\n(STAR briefings)"]
    G -->|"interview_prep.json"| F
```

### Artifact Chain

| Phase | Reads | Writes |
|:---|:---|:---|
| 1. Intake | `data/raw_resumes/*.pdf` | `data/profiles/profile.json` |
| 2. Sourcing | `config/searches.yaml` | `data/outputs/scraped_jobs.json`, `delta_store.db` |
| 3. Evaluation | `profile.json`, `scraped_jobs.json` | `evaluated_jobs.json`, `qualified_jobs.json` |
| 4. Tailoring | `profile.json`, `qualified_jobs.json` | `manifest.json`, `tailored_resumes/*.pdf` |
| 5. Auto-Apply | `profile.json`, `manifest.json` | `application_results.json` |
| 6. Tracking | `application_results.json`, `qualified_jobs.json` | `applications_tracker.xlsx`, `application_pack.zip` |
| 7. Interview Prep | `profile.json`, `qualified_jobs.json` | `data/outputs/interview_prep.json` |

---

## 4. Anti-Hallucination Safety System

Four cryptographic and algorithmic gates ensure zero fact mutation:

1. **Gate 1 — Intake (`validator.py`)**: Quantifiable achievements are isolated by regex, audited against the raw resume string, and hashed into an order-independent SHA-256 seal (`CandidateProfile.fact_hash`).
2. **Gate 2 — Tailoring (`rewriter.py`)**:
   - *Restoration*: Dropped locked metrics are automatically reinstated.
   - *Fabrication Gate*: Bullets with unverified numbers revert to the closest original bullet or are dropped.
3. **Gate 3 — Form Filling (`form_filler.py`)**: Fields without a backing candidate fact are left blank and reported in `skipped_fields`. No automated guessing or speculative answers.
4. **Gate 4 — Verification (`python main.py verify`)**: Phases 4, 5, 6, and 7 verify the SHA-256 seal before execution; tampered profiles are rejected immediately.

---

## 5. Configuration & Settings

`settings.py` provides typed configuration via `pydantic-settings`:
- **Auto-directory provisioning**: `settings.ensure_directories()` initializes all data and output folders on import.
- **Provider selection**: Automatically selects Groq, OpenAI, or Anthropic based on configured keys, defaulting gracefully to deterministic heuristic fallbacks if no keys are provided.
- **Privacy enforcement**: Forces `ANONYMIZED_TELEMETRY="false"` and `POSTHOG_DISABLED="1"` in `os.environ` to block telemetry leaks.

---

## 6. Pydantic Schema Reference

Key models in `schema.py` (all inherit from `StrictModel` with `extra="forbid"`):
- `CandidateProfile`: Complete candidate profile, contact info, employment, skills, projects, and SHA-256 seal.
- `JobPosting`: Normalized posting containing title, company, location, URL, description, salary bands, work mode, and contacts.
- `EvaluationScore`: Tier 1 embedding similarity, Tier 2 fit score, technical and seniority breakdown, reasoning, and skill overlap.
- `EvaluatedJob`: Composition of `job: JobPosting` and `evaluation: EvaluationScore`.
- `TailoredResumeRecord`: Audit record documenting tailored PDF path, fit score, restored metrics, and blocked fabrications.
- `ApplicationOutcome`: Result of browser form automation or fallback routing.

---

## 7. Phase 1 — Intake & Readiness
- `parser.py`: Multi-strategy extraction ladder (LlamaParse → pdfplumber → pypdf → LLM → deterministic heuristic).
- `heuristic.py`: 1007-line deterministic parser using regex to extract contact info, experience, education, and categorized skills with zero AI cost.
- `readiness.py`: Pre-flight diagnostic score (0-100) reporting blockers (-25) and warnings (-7) without side effects.
- `validator.py`: Enriches metrics (including Indian denominations ₹ crore/lakh), audits against source text, and seals with SHA-256.

---

## 8. Phase 2 — Sourcing & Dedup
- `scraper.py`: Omnichannel scraper using `python-jobspy` across LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Bayt, Naukri, and BDJobs.
- `ats_direct.py`: Direct public API feeds from Greenhouse, Lever, and Ashby boards, bypassing search aggregator rate limits.
- `delta_store.py`: Persistent SQLite store in WAL mode ensuring each job is evaluated, tailored, and applied to exactly once.
- `proxy_manager.py`: Sticky residential proxy rotation per board with automatic failure handling.

---

## 9. Phase 3 — Evaluation & Scoring
- **Tier 1**: `embedder.py` runs fast, free cosine similarity filtering using `sentence-transformers/all-MiniLM-L6-v2` (or TF-IDF fallback).
- **Tier 2**: `reranker.py` scores candidates 1.0–10.0 using Groq (`openai/gpt-oss-120b`), OpenAI (`gpt-4o`), Anthropic, or an offline heuristic judge.
- **Atomic Checkpointing**: Saves progress to `evaluation_checkpoint.json` so interrupted runs resume seamlessly without re-scoring previously evaluated jobs.

---

## 10. Phase 4 — Tailoring & Cover Letters
- `rewriter.py`: Role-specific bullet point tailoring with strict metric integrity enforcement.
- `compiler.py`: Typst Rust-backed compiler generating single-column ATS-friendly PDFs in ~100ms.
- `cover_letter.py`: Generates matching grounded one-page cover letters compiled via `cover_letter.typ`.
- `regional.py`: Applies country-specific formatting (US, UK, India, Canada, Germany) and evaluates remote work authorization.

---

## 11. Phase 5 — Auto-Apply & Workday Assist
- `agent.py`: Step-bounded browser automation loop (capped at 25 steps, max 3 consecutive errors).
- `browser_session.py`: Persistent Chromium user profile preserving cookies and session logins across runs.
- `form_filler.py`: DOM perception and factual question answering. Unknown questions are skipped, never guessed. Refuses to fill demographic/voluntary-disclosure fields (race, veteran status, disability, gender) on any channel.
- `hitl.py`: Human-in-the-loop pause for CAPTCHA and MFA challenges.
- `routing.py`: Detects Workday listings and routes them to the assisted flow (`workday-assist` CLI command) instead of the normal auto-apply path; there is no separate `workday.py` module. Assisted mode fills the form and always stops before submit — it never clicks it.

---

## 12. Phase 6 — Tracking, Warm Contacts & Bundles
- `tracker.py`: `applications_tracker.xlsx` master workbook with navy headers, priority color fills, and idempotent rows via hidden Job ID column.
- `contacts/warm.py` (a separate top-level package, not `tracking/contacts.py`): reads an employer's public team/about pages for named staff with a visible title, as a possible referral lead. Never guesses an email address or pattern, never crawls social platforms, never claims a relationship — every result is labelled "unverified public team lead."
- `tracking/inbox.py`: Opt-in, read-only IMAP sync that classifies replies (rejection/interview/offer/other) and updates tracking records. Requires four env vars to be set explicitly; never sends mail.
- `bundle.py`: Assembles `application_pack.zip` containing an interactive HTML dashboard, verified PDFs, CSV exports, and SHA-256 hash manifest.
- `quality.py`: Computes an end-to-end quality score out of 10 for profile integrity, freshness, document coverage, and tracking readiness.
- `supplements.py`: Cross-references interview-prep/cover-letter manifests and warm-contact leads against actual files on disk (re-verifying the profile hash) before exposing a link, so a stale document from a superseded profile is hidden.

---

## 13. Phase 7 — Interview Prep & STAR Briefings
- `interview/pipeline.py` (the module is `interview`, **not** `prep`): `InterviewPrepPipeline.run()` generates 10 questions per qualified job — 4 technical, 3 behavioral, 3 company-fit.
- **STAR Evidence Mapping**: Behavioral answers are built only from bullets/locked facts already in the sealed profile, via the same `enforce_metric_integrity` gate tailoring uses (through the shared `generation.py` helpers) — an answer cannot claim anything the resume doesn't.
- **Role Briefings**: A "likely panel composition" line is explicitly labelled a hypothesis, not confirmed company information.
- **Offline Fallback**: `prep --offline` selects evidence without any provider call.

---

## 14. Web UI (Visual Flow Console)
- **Zero-framework architecture**: Standard library `http.server.ThreadingHTTPServer`.
- **Security controls**: Loopback-only binding (`127.0.0.1`), Windows `SO_EXCLUSIVEADDRUSE`, per-session CSRF tokens, Host/Origin DNS-rebinding protection, and explicit `"confirm_live": "APPLY"` verification.
- **Live progress streaming**: Real-time Server-Sent Events (SSE) via `_LineTee` pipe.
- **Interactive UI**: n8n-style node graph across all 7 phases, first-run resume wizard, live log viewer, and settings manager.

---

## 15. CLI Reference (26 Subcommands + `db` group)

```powershell
# Core Lifecycle
python main.py intake [--resume FILE]          # Phase 1: Parse & seal profile
python main.py check [RESUME]                  # Phase 1: Pre-flight readiness diagnostic
python main.py configure                      # Phase 1: Interactive search parameters
python main.py verify                         # Phase 1: Check SHA-256 fact seal
python main.py source [--no-ats]              # Phase 2: Omnichannel job scraping
python main.py evaluate [--threshold T]        # Phase 3: Two-tier semantic evaluation
python main.py tailor [--cover-letter]         # Phase 4: ATS PDF tailoring & cover letters
python main.py apply [--dry-run]              # Phase 5: Browser automation execution
python main.py track [--all]                   # Phase 6: Excel tracker & cold outreach
python main.py prep [--offline]                # Phase 7: Interview prep & STAR evidence
python main.py run-pipeline [--dry-run]        # Phases 1-7 end-to-end execution
python main.py daily [--limit N]              # Daily search-to-download pack workflow

# Specialized Tools & Operations
python main.py ui [--port 8765]                # Launch Web Visual Flow Console
python main.py quality                         # Score system data & document quality (/10)
python main.py export [--bundle]               # Export CSV or portable application pack ZIP
python main.py contacts [--limit N]            # Discover team leads & hiring contacts
python main.py preferences                     # Set work modes & country preferences
python main.py sync-inbox                      # Read IMAP folder for application outcomes
python main.py workday-assist                  # Assistive browser fill for Workday portals
python main.py db [QUERY]                      # Query relational jobs cache
python main.py db-check                        # Verify hosted queue database
python main.py hosted-key                      # Generate/inspect hosted control plane tokens
python main.py production-check               # Self-hosted production deployment audit
python main.py doctor [--live]                 # Check dependencies, keys & browser
python main.py status                          # Full pipeline state summary
python main.py reset [--delta] [--outputs]     # Clear delta store or generated artifacts
```

---

## 16. Environment Variables

Configured via `.env` file (see `.env.example`):
- `DEFAULT_LLM_PROVIDER`: `groq`, `openai`, `anthropic`, or `none`.
- `GROQ_API_KEY`: Groq cloud API key.
- `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `LLAMA_CLOUD_API_KEY`: Optional provider keys.
- `SEMANTIC_EMBEDDING_MODEL`: `sentence-transformers/all-MiniLM-L6-v2`.
- `TIER1_THRESHOLD`: Cosine similarity cutoff (default `0.15`).
- `MIN_MATCH_SCORE`: LLM judge cutoff (default `7.0`).
- `PLAYWRIGHT_HEADLESS`: Run browser headlessly (`false` for visible debugging).
- `MAX_APPLICATION_STEPS`: Hard ceiling per application (default `25`).
- `REQUIRE_APPLY_CONFIRMATION`: Require explicit confirmation before live apply (`true`).
- `ANONYMIZED_TELEMETRY`: Forced to `false` across all libraries for candidate privacy.

---

## 17. Test Suite (519 Tests)

Run with `python -m pytest -v`:
- **Current status**: 500 passed, 6 skipped (browser integration tests opt-in via `JOB_AGENT_BROWSER_TESTS=1`).
- **Test execution time**: ~2.5 minutes across 34 test modules.
- **Key test modules**:
  - `test_anti_hallucination.py`: Fact sealing, intake audit, tailoring gate, cold email verification.
  - `test_application_pack.py`: Portable ZIP bundle generation and document hash validation.
  - `test_workflow.py`: Seven-phase workflow coordinator and daily command runner.
  - `test_web.py`: HTTP security headers, CSRF token validation, loopback origin checks, SSE streaming.
  - `test_readiness.py`: Resume format diagnostics, scoring penalties, actionable recommendations.
  - `test_tailoring.py`: Typst compilation, metric preservation, and fabrication blocking.

---

## 18. Key Design Invariants

> [!IMPORTANT]
> These invariants are strictly enforced by automated tests across the codebase:

1. **Zero Fact Invention**: Every candidate statement must be copied from the resume or arithmetically derived.
2. **Cryptographic Fact Seal**: Locked achievements are hashed via SHA-256; tampered profiles halt execution.
3. **Derived Qualification**: `passed_threshold` is always derived programmatically from `fit_score >= threshold_used`, never delegated to LLMs.
4. **Derived Application Status**: `applied` is strictly derived from `status == "applied"`, preventing dry runs from contaminating records.
5. **Single Submit Attempt**: Browser automation clicks submit at most once per job to prevent duplicate applications.
6. **Deterministic Offline Fallbacks**: Every phase functions completely without paid API keys.
7. **Semicolon Metric Separation**: Metric values use `"; "` delimiters to prevent thousand-separators from breaking.
8. **Idempotent Bookkeeping**: Relational and Excel trackers use job IDs to update existing rows rather than creating duplicate entries.
9. **Exclusive Process Lock**: CLI and UI pipeline runs enforce exclusive locks to prevent corrupting disk artifacts.
10. **Loopback & Privacy**: Web UI binds only to loopback; all external telemetry is unconditionally disabled.
