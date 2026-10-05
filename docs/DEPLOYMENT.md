# Deployment Guide

This project is safe to run as a local or self-hosted personal job agent today.
Do not expose the included flow console directly to the public internet. It can
start browser automation and submit real applications, so it deliberately binds
to loopback and uses local files for state.

## Supported deployment modes

| Mode | Best for | How it runs |
| --- | --- | --- |
| Local dashboard | One candidate on their own machine | `python main.py ui` |
| Local CLI | One candidate, repeatable terminal runs | `python main.py run-pipeline --dry-run --skip-intake` |
| Docker CLI | Self-hosted personal worker or scheduled run | `docker compose run --rm job-agent python main.py status` |
| Hosted API scaffold | A safe public control plane smoke test | `docker compose -f docker-compose.hosted.yml up --build` |
| Public SaaS | Many users | Hosted API + real auth, per-user storage, managed queue, isolated browser workers |

## Opening the dashboard from your other devices (private, not public)

The flow console still binds to loopback only (`run_server` refuses any other
host) — that has not changed and should not change, because it can start
browser automation and submit real applications under your identity. To reach
it from your phone or another computer without putting it on the open
internet, run it through a private tunnel *you* control, and require a login
in front of it.

**Already set up once and just want to start it?** Run
`.\scripts\start_dashboard.ps1` — it points `tailscale serve` at the
dashboard's port, warns if `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` aren't
set, prints the tailnet HTTPS URL, and starts the dashboard. The one-time
setup below still has to happen first (installing Tailscale, signing in,
setting the login, and enabling Serve on your tailnet the first time it asks).

1. **Set a login.** In `.env`:

   ```env
   DASHBOARD_USERNAME=your-name
   DASHBOARD_PASSWORD=a-long-random-password
   ```

   With both set, every request needs this login (HTTP Basic Auth) on top of
   the existing loopback bind and per-session token — without them, the
   console behaves exactly as before (no login prompt).

2. **Start the dashboard as usual:**

   ```powershell
   python main.py ui
   ```

3. **Install Tailscale** (a private mesh network between only your own
   devices; free for personal use) and sign in — this step needs your own
   browser and account, so it can't be automated on your behalf:

   ```powershell
   winget install Tailscale.Tailscale
   tailscale up
   ```

   `tailscale up` opens a browser for you to log in (Google/Microsoft/GitHub/
   email). Do this on the same machine running the dashboard.

4. **Serve the loopback port over your tailnet:**

   ```powershell
   tailscale serve --bg http://127.0.0.1:8765
   ```

   (Older Tailscale versions use `tailscale serve https / http://127.0.0.1:8765`
   — if the command errors naming the syntax that changed, use the form it
   suggests.) The first time you do this, Tailscale may print a link to
   *enable Serve on your tailnet* — that's a one-time, one-click account
   setting, then re-run the command above.

   This gives you an HTTPS URL like `https://your-device.your-tailnet.ts.net`,
   reachable only from devices signed into your own Tailscale account — not
   the public internet. Run `tailscale serve status` to see it, and
   `tailscale serve reset` to stop sharing it.

5. **Add that hostname to `.env`** so the app's own Host/Origin check accepts
   it (this check exists to block DNS-rebinding attacks, so it only accepts
   hostnames you explicitly list):

   ```env
   DASHBOARD_ALLOWED_HOSTS=your-device.your-tailnet.ts.net
   ```

   Restart `python main.py ui` after changing `.env`.

6. **Open that HTTPS URL from your phone or laptop**, signed into the same
   Tailscale account, and log in with the username/password from step 1.

7. **Make it survive a reboot or a crash, so you stop having to notice and
   restart it:**

   ```powershell
   .\scripts\install_autostart.ps1
   ```

   Registers a `JobAgentDashboard` Windows scheduled task that runs
   `start_dashboard.ps1` at logon, **and again every 15 minutes forever** as a
   safety net. The 15-minute check matters because a laptop that mostly sleeps
   instead of fully logging out won't re-fire an "at logon" trigger for days —
   confirmed the hard way: `tailscale serve` kept proxying to port 8765 for
   three days after the dashboard process had died, silently returning `502`
   to anyone who tried the URL, because nothing was left to notice and restart
   it. Windows Task Scheduler's own "restart the task if it fails" setting was
   tested against this same task and did not work (it did not restart a killed
   process even after minutes, with the setting configured correctly) — that's
   a known unreliability in Task Scheduler for logon-triggered tasks, not a
   configuration mistake, which is why `start_dashboard.ps1` has its own
   internal retry loop and the 15-minute trigger exists as a second, unrelated
   safety net rather than depending on that setting working.

   Remove it later with `Unregister-ScheduledTask -TaskName JobAgentDashboard -Confirm:$false`.

Prefer Cloudflare Tunnel instead of Tailscale? The same steps 1, 2 and 5 apply
— only step 3-4 change to `cloudflared tunnel --url http://127.0.0.1:8765`
(quick tunnels) or a named tunnel with Cloudflare Access in front for login at
the edge too. Either way: **never** set `DASHBOARD_ALLOWED_HOSTS` without also
setting `DASHBOARD_USERNAME`/`DASHBOARD_PASSWORD` — the server prints a
warning at startup if you do, because that combination would let anyone who
can reach the tunnel hostname use the console with no login at all.

## Local production run

1. Install Python 3.10 or newer.
2. Create the environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   python -m playwright install chromium
   ```

3. Copy the environment file:

   ```powershell
   copy .env.example .env
   ```

4. Set optional keys in `.env`:

   ```env
   DEFAULT_LLM_PROVIDER=groq
   GROQ_API_KEYS=gsk_first,gsk_second,gsk_third
   HUNTER_API_KEY=
   RESIDENTIAL_PROXY_URL=
   ```

5. Check the installation:

   ```powershell
   python main.py doctor --live
   python main.py production-check
   ```

6. Import a real resume:

   ```powershell
   python main.py check "C:\Users\you\Desktop\resume.pdf"
   python main.py intake --resume "C:\Users\you\Desktop\resume.pdf"
   ```

7. Set candidate preferences:

   ```powershell
   python main.py preferences --country India --authorized India --sponsorship --remote-worldwide --salary 1000000 --salary-max 1400000 --currency INR
   ```

8. Configure search targets in the dashboard or in `config/searches.yaml`.
9. Run a full safe rehearsal:

   ```powershell
   python main.py run-pipeline --skip-intake --dry-run --limit 10
   ```

10. Review outputs:

   - `data/outputs/jobs_master.csv`
   - `data/outputs/applications_tracker.xlsx`
   - `data/outputs/outreach/*.eml`
   - `data/outputs/tailored_resumes/*.pdf`

## Docker CLI run

Build and run the production check:

```powershell
docker compose build
docker compose run --rm job-agent
```

The main compose file starts Postgres automatically and persists it in the
`postgres-data` Docker volume. The app receives:

```env
DATABASE_URL=postgresql://job_agent:job_agent_dev_password@postgres:5432/job_agent
POSTGRES_DB=job_agent
POSTGRES_USER=job_agent
POSTGRES_PASSWORD=job_agent_dev_password
```

Override those values in `.env` or your host's secret manager before deploying
outside a local machine.

Run commands inside the container:

```powershell
docker compose run --rm job-agent python main.py doctor
docker compose run --rm job-agent python main.py status
docker compose run --rm job-agent python main.py db-check
docker compose run --rm job-agent python main.py source
docker compose run --rm job-agent python main.py run-pipeline --skip-intake --dry-run --limit 10
```

The compose file mounts `./data` and `./config`, so generated outputs and search
settings stay on the host.

Open the database viewer:

```text
http://127.0.0.1:8081
```

Use these Adminer values:

| Field | Value |
| --- | --- |
| System | PostgreSQL |
| Server | `postgres` |
| Username | `job_agent` |
| Password | `job_agent_dev_password` |
| Database | `job_agent` |

If Adminer shows `SQLSTATE[08006] could not translate host name "db"`, use
`postgres` in the Server field. The compose files also provide `db` as a
compatibility alias for tools that expect that hostname, but `postgres` is the
canonical service name in this project.

The persistent database files live inside Docker's `postgres-data` volume. To
inspect the volume:

```powershell
docker volume ls
docker volume inspect jobagent_postgres-data
```

The Docker image is meant for CLI and scheduled runs. The dashboard remains a
local loopback UI by design; binding it to `0.0.0.0` would make a browser-driving
application tool reachable from other machines.

## Hosted API and worker smoke test

The authentication slice now uses per-user bearer keys. The old shared
`HOSTED_API_TOKEN` is no longer accepted. Issue a key from the administrator's
terminal against the same database used by the API:

```powershell
python main.py hosted-key --user candidate-1
# For the compose deployment:
docker compose -f docker-compose.hosted.yml exec hosted-api python main.py hosted-key --user candidate-1
```

Copy the returned key once into your client or secret manager. Only its SHA-256
hash is stored. Rotate by issuing a new key; revoke an old key with
`python main.py hosted-key --revoke <key-id-before-the-dot>` in the same environment.
There is no public account-provisioning endpoint.

The API derives ownership from this key. A supplied `user_id` must match;
another user's run returns 404. `/v1/jobs` and `/v1/jobs/stats` read only the
`hosted_user_jobs` partition for that owner. They never expose the shared personal
jobs database. Existing personal jobs are not migrated automatically. The new
`hosted_users`, `hosted_api_keys` and `hosted_user_jobs` tables are created on the
configured Postgres database, or on SQLite for local tests.
When creating runs from a client that may retry, send an `Idempotency-Key`
header. The queue stores one run per authenticated user and key, so a repeated
`POST /v1/runs` returns the original row instead of enqueueing duplicate work.

This completes authentication, API data isolation, typed FastAPI/OpenAPI routes,
trusted-origin CORS, request IDs, and in-process rate limiting. The reference
worker defaults to validation mode, and can execute queued phases only when
`HOSTED_WORKER_EXECUTE=1` is set. Execution happens in a subprocess with
per-user data, output, artifact, and browser-profile directories under
`HOSTED_WORKER_WORKSPACE_DIR`; the worker also passes
`JOB_AGENT_HOSTED_USER_ID` so normalized `candidate_profiles` and downstream
candidate-owned rows are scoped to the authenticated hosted account, not just
the resume email. After a successful executed run, the worker reads the
workspace/Postgres jobs DB and publishes those rows into `hosted_user_jobs`, so
`/v1/jobs` exposes the completed run's results only to that hosted user. The
worker writes structured lifecycle events to `run_events` with run IDs like
`hosted:<id>` for validation, execution start, job publication, success, and
failure. The hosted API exposes those events only to the authenticated owner via
`/v1/runs/<id>/events`. Object storage, container isolation, and external
alerting remain required before executing real hosted browser automation for
many users.

The repository now includes a FastAPI hosted control plane that is safe to put
behind HTTPS because it does not expose the local dashboard and does not run
browser automation in the request thread. The older standard-library module
remains for compatibility smoke tests, but compose runs uvicorn against
`job_agent.hosted.fastapi_app:app`.

It has two services:

- `hosted-api`: token-protected `/v1/jobs`, `/v1/jobs/stats`,
  `POST /v1/runs`, `GET /v1/runs`, `GET /v1/runs/<id>`, and
  `GET /v1/runs/<id>/events` endpoints, plus `/health`, `/ready`, and
  `/openapi.json`. Run listing is scoped to the authenticated owner and accepts
  `status` plus `limit` filters.
- `hosted-worker`: claims queued runs, heartbeats while running, and either
  completes validation rows or executes phases in a per-user subprocess when
  `HOSTED_WORKER_EXECUTE=1`.

Start it locally:

```powershell
docker compose -f docker-compose.hosted.yml up --build
```

In another terminal, enqueue a dry-run request:

```powershell
$token = "paste-the-per-user-key-issued-above"
$body = @{
  user_id = "candidate-1"
  phases = @("source", "evaluate", "tailor", "track")
  options = @{ dry_run = $true; limit = 3 }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/v1/runs" `
  -Headers @{ Authorization = "Bearer $token"; "Idempotency-Key" = "candidate-1-source-evaluate-tailor-track-001" } `
  -ContentType "application/json" `
  -Body $body
```

Check readiness and queue counts:

```powershell
Invoke-RestMethod "http://127.0.0.1:8080/ready"
```

For a real hosted product, store issued per-user keys securely and put the API
behind TLS. Keep live apply disabled unless the worker runs inside a per-user
isolated browser container and the user has explicitly authorized submission:
the queue requires `allow_live_apply=true`, and the worker additionally requires
`HOSTED_WORKER_ALLOW_LIVE_APPLY=1`. In `staging` or `production`, the worker
also refuses live apply unless `HOSTED_WORKER_ISOLATION_MODE=container`; the
default `workspace` mode is only for local validation and smoke tests.

## What a public hosted version needs

For many concurrent users, split the product into these services:

1. Web app with login, CSRF protection, rate limiting, and per-user project IDs.
2. Object storage for resumes, generated PDFs, CSVs, and `.eml` drafts.
3. Database tables scoped by user for profiles, jobs, deltas, applications, and outreach logs.
4. Queue service for long phases: source, evaluate, tailor, apply, and track.
5. Isolated browser workers. Run one browser profile per user and never share cookies.
6. Secret storage for each user's API keys. Never store keys in repo files or logs.
7. Per-provider throttles for Groq, Hunter, and job-board requests.
8. Observability for phase duration, failures, source block rates, email-draft counts, and apply outcomes.

SQLite works well for one local agent. A public hosted version should move the
state in `delta_store.db` to Postgres or another managed relational database and
run phase work through a queue such as Celery, RQ, Sidekiq, Cloud Tasks, or a
host equivalent.

The included `docker-compose.hosted.yml` carries optional `postgres` and `redis`
services under the `saas` profile to show that final shape:

```powershell
docker compose -f docker-compose.hosted.yml --profile saas up --build
```

The hosted path uses SQLite only when `DATABASE_URL` is absent so the scaffold
works on any developer machine; with `DATABASE_URL`, `HostedQueue` uses
Postgres and claims work with row locks. Before serving many real users, run
each worker in an isolated container scoped to one user and point artifacts at
S3-compatible storage.

## Database choices

The local product uses SQLite files under `data/outputs/`:

| File | Used for |
| --- | --- |
| `delta_store.db` | Seen jobs, lifecycle status, application attempts, outreach no-repeat ledger |
| `hosted_queue.db` | Reference hosted API queue for smoke tests |
| `jobs.db` | Every fetched job, its contact emails and its outreach drafts, for querying |
| Postgres `hosted_runs` table | Hosted API queue when `DATABASE_URL` is set |
| Postgres `jobs`, `job_contacts`, `job_outreach` | The same fetched-job data when `DATABASE_URL` is set |

With `DATABASE_URL` set, both the queue and the fetched jobs live in Postgres, so
one connection answers "who ran what" and "what did it find". The tables are
created identically on SQLite, so a query written against one works on the other.
`job_overview` is a view with one row per job and its best contact email.

The database is a queryable *view* of what the pipeline wrote, rebuilt after every
phase from the artifacts and the cumulative jobs CSV. Deleting it loses nothing:
`python main.py db sync` rebuilds it.

SQLite is the right default for one user because it is zero-setup, portable, and
keeps all personal data on the user's machine.

Postgres is included in both compose files. In the main compose file it is the
database service exposed through `DATABASE_URL`; in `docker-compose.hosted.yml`
it appears under the optional `saas` profile with Redis to show the full hosted
shape. The hosted run queue uses Postgres automatically when `DATABASE_URL` is
set. A real hosted deployment should move these logical tables into Postgres:

- users and projects
- resumes and sealed candidate profiles
- search configurations and candidate preferences
- jobs, job fingerprints, status transitions, and application attempts
- outreach drafts and the no-repeat ledger
- queued runs and worker leases

Recommended managed Postgres hosts include Supabase, Neon, Railway, Render, Fly
Postgres, AWS RDS, Google Cloud SQL, and Azure Database for PostgreSQL.

For a serious public deployment, put files such as resumes, PDFs, spreadsheets,
and `.eml` drafts in object storage instead of the database. Good options are S3,
Cloudflare R2, Google Cloud Storage, Azure Blob Storage, and Supabase Storage.
`production-check` now treats local artifact storage as acceptable for local
smoke tests only; in `staging` or `production`, configure
`ARTIFACT_STORAGE_BACKEND=s3` plus `ARTIFACT_S3_BUCKET` before considering the
deployment ready.

The safe migration path is:

1. Keep local SQLite for the open-source personal agent.
2. Use the hosted API scaffold for smoke testing queue/API behavior.
3. Set `DATABASE_URL` so `HostedQueue`, `JobsDatabase`, and `DeltaStore` use
   Postgres for queued runs, jobs, normalized candidate state, seen jobs,
   application attempts, and outreach ledgers.
4. Set `ARTIFACT_STORAGE_BACKEND=s3` and point `ARTIFACT_S3_BUCKET`/
   `ARTIFACT_S3_PREFIX` at a durable per-environment bucket or prefix.
5. Run every queued job in a per-user worker container with its own mounted data
   directory or object-storage prefix.
6. Store API keys in a managed secret store, never in shared project files.

To verify the Postgres path against a real database, point the opt-in
integration test at a disposable database. It creates and drops its own schema
and covers the jobs DB, DeltaStore, artifact verification, run events, hosted
queue, and hosted auth tables on the same Postgres database:

```powershell
$env:JOB_AGENT_POSTGRES_TEST_URL='postgresql://user:pass@localhost:5432/job_agent_test'
python -m pytest tests/test_postgres_integration.py -q
```

## Operations

### Queue inspection and recovery

Hosted runs are durable rows. The worker claims with `FOR UPDATE SKIP LOCKED`
on Postgres and records attempts plus heartbeat timestamps. Use these commands
instead of manual SQL for routine checks:

```powershell
python main.py queue status
python main.py queue status --status running
python main.py queue recover-stale --older-than 300
python main.py db runs
python main.py db events --limit 50
```

`recover-stale` moves abandoned `running` rows back to `retryable` while attempts
remain, or to `failed` after `max_attempts`.

### Backup and restore

For production, back up the Postgres database and the artifact/object-storage
bucket together. The database contains metadata, hashes, application history,
queue state, and run events; object storage contains the generated documents.

Example Postgres backup:

```powershell
$env:PGPASSWORD = "your-password"
pg_dump --format=custom --file job_agent_$(Get-Date -Format yyyyMMdd_HHmmss).dump `
  --dbname "postgresql://job_agent@host:5432/job_agent"
```

Example restore validation into a disposable database:

```powershell
createdb job_agent_restore_check
pg_restore --clean --if-exists --dbname job_agent_restore_check .\job_agent_YYYYMMDD_HHMMSS.dump
$env:DATABASE_URL = "postgresql://job_agent@host:5432/job_agent_restore_check"
python main.py db migrations
python main.py db integrity
python main.py db artifacts verify
```

For S3-compatible artifacts, enable versioning/lifecycle retention on the
bucket and periodically run `python main.py db artifacts verify` in the
restored environment. It checks every `resume_artifacts.object_key` it can read
against stored `sha256` and size metadata, and checks local
`interview_prep_artifacts.file_path` guide hashes plus
`cover_letter_artifacts.file_path` PDF hashes. Do not treat an untested backup
as valid.

### Retention

Preserve indefinitely unless the user explicitly asks to purge:

- submitted applications
- application events
- interview/offer/rejection history
- evaluation history tied to a candidate/profile hash
- run events needed to explain production failures

Safe to archive or expire by policy:

- stale unqualified jobs with no candidate interaction
- failed temporary runs after the operator has reviewed them
- raw source payload caches after the retention window
- temporary generated artifacts superseded by newer verified artifacts

Keep retention windows configurable in deployment automation rather than
hardcoding destructive deletion in the application.

## Email status

The agent does not send emails. It creates unsent `.eml` drafts and records them
in the outreach ledger. Use these commands to see the current state:

```powershell
python main.py status
python main.py export
```

`status` reports how many drafts exist and how many unique inboxes are in the
ledger. `jobs_master.csv` shows the recipient, subject, body, draft file, and
source of the email address for each job.

If no email is found for a job, that is normal. Many companies do not publish
hiring mailboxes. The row still keeps the careers or application URL, and the
draft body can be used manually on LinkedIn or an application portal.


