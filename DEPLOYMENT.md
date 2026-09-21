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

The repository now includes a hosted control-plane scaffold that is safe to put
behind HTTPS because it does not expose the local dashboard and does not run
browser automation in the request thread.

It has two services:

- `hosted-api`: token-protected `/health`, `/ready`, `POST /runs`, and
  `GET /runs/<id>` endpoints.
- `hosted-worker`: claims queued runs and completes them in validation mode.

Start it locally:

```powershell
docker compose -f docker-compose.hosted.yml up --build
```

In another terminal, enqueue a dry-run request:

```powershell
$token = "change-this-token-before-deploy"
$body = @{
  user_id = "local-smoke-user"
  phases = @("source", "evaluate", "tailor", "track")
  options = @{ dry_run = $true; limit = 3 }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8080/runs" `
  -Headers @{ Authorization = "Bearer $token" } `
  -ContentType "application/json" `
  -Body $body
```

Check readiness and queue counts:

```powershell
Invoke-RestMethod "http://127.0.0.1:8080/ready"
```

For a real hosted product, replace the placeholder token with a secret from your
hosting provider and put the API behind TLS. Keep the reference worker in
validation mode until you have per-user storage and a per-user browser profile.

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

The reference API still uses the local SQLite queue so the scaffold works on any
developer machine. Before serving many real users, replace `HostedQueue` with
Postgres or Redis-backed queue storage and run each claimed job inside an
isolated worker container scoped to one user.

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

The safe migration path is:

1. Keep local SQLite for the open-source personal agent.
2. Use the hosted API scaffold for smoke testing queue/API behavior.
3. Add Postgres tables that mirror `DeltaStore` and `HostedQueue`.
4. Run every queued job in a per-user worker container with its own mounted data
   directory or object-storage prefix.
5. Store API keys in a managed secret store, never in shared project files.

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


