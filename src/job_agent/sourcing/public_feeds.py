"""Public job-board API ingestion for broad, low-friction coverage.

These adapters use documented JSON endpoints and never scrape protected pages.
Each source can fail independently without ending the sourcing sweep.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import hashlib
import json
import time

import requests
from rich.console import Console

from job_agent.config.normalize import clean_text, strip_html
from job_agent.config.schema import JobPosting, SearchParameters
from job_agent.contacts.extract import job_post_contacts
from job_agent.runtime import check_cancelled, RunCancelled
from job_agent.config.settings import settings
from job_agent.sourcing.relevance import title_matches

console = Console()


class PublicFeedIngestion:
    """Fetch Remotive and Arbeitnow listings through their public JSON APIs."""

    def __init__(self, timeout: int = 20, session: Optional[requests.Session] = None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.errors: List[str] = []
        self.report: Dict[str, Any] = {}
        self.cached_requests = 0
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "AutonomousJobAgent/0.2 (+local personal job search)",
        })

    def _get_json(self, url: str, *, params: Optional[Dict[str, Any]] = None) -> Optional[Any]:
        key = hashlib.sha256(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
        cache = settings.outputs_dir / "feed_cache" / f"{key}.json"
        ttl = 6 * 3600 if "remotive.com" in url else 3600
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
            if 0 <= time.time() - saved["at"] < ttl:
                self.cached_requests += 1
                return saved["data"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            response = self.session.get(url, params=params, timeout=self.timeout)
            response.raise_for_status()
            data = response.json()
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"at": time.time(), "data": data}), encoding="utf-8")
            except OSError:
                pass
            return data
        except (requests.RequestException, ValueError) as exc:
            self.errors.append(f"{url}: {exc.__class__.__name__}")
            console.print(f"[dim]Public feed request failed for {url}: {exc.__class__.__name__}.[/dim]")
            return None

    @staticmethod
    def _build(**values: Any) -> Optional[JobPosting]:
        try:
            return JobPosting(**values)
        except Exception as exc:
            console.print(f"[dim]Skipped malformed public-feed listing: {str(exc)[:140]}[/dim]")
            return None

    def fetch_remotive(self, params: SearchParameters) -> List[JobPosting]:
        """Fetch recent remote roles once, then match target titles locally.

        Remotive asks public clients to poll sparingly. One broad request per
        sweep avoids multiplying traffic by the number of target roles.
        """
        jobs: List[JobPosting] = []
        check_cancelled()
        # Pull enough of the newest feed for several narrow target titles. The
        # response remains a single request and results are capped locally.
        limit = 500
        data = self._get_json("https://remotive.com/api/remote-jobs", params={"limit": limit})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            self.errors.append("Remotive did not return a jobs list")
            return jobs
        for item in data.get("jobs", []):
            if not isinstance(item, dict):
                continue
            title = clean_text(item.get("title"))
            company = clean_text(item.get("company_name"))
            url = clean_text(item.get("url"))
            if not title or not company or not url or not title_matches(title, params.target_domains):
                continue
            description = strip_html(item.get("description", ""))
            posting = self._build(
                id=JobPosting.create_id(url, company, title),
                title=title,
                company=company,
                location=clean_text(item.get("candidate_required_location")) or "Remote",
                job_url=url,
                apply_url=url,
                description=description,
                date_posted=clean_text(item.get("publication_date")) or None,
                is_remote=True,
                work_mode="remote",
                job_type=clean_text(item.get("job_type")) or None,
                salary_currency=None,
                source="remotive",
                contacts=job_post_contacts(description),
            )
            if posting:
                jobs.append(posting)
            if len(jobs) >= params.max_results_per_board:
                break
        return jobs

    def fetch_arbeitnow(self, params: SearchParameters) -> List[JobPosting]:
        """Fetch the current Arbeitnow feed and filter it locally by target title."""
        jobs: List[JobPosting] = []
        page = 1
        # The API is a general feed rather than a search endpoint. A few pages give
        # useful coverage without turning a personal search into an unbounded crawl.
        while page <= 3 and len(jobs) < params.max_results_per_board:
            check_cancelled()
            data = self._get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
            if not isinstance(data, dict) or not isinstance(data.get("data"), list):
                self.errors.append("Arbeitnow did not return a data list")
                break
            rows = data.get("data") or []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                title = clean_text(item.get("title"))
                company = clean_text(item.get("company_name"))
                url = clean_text(item.get("url"))
                if not title or not company or not url or not title_matches(title, params.target_domains):
                    continue
                description = strip_html(item.get("description", ""))
                remote = bool(item.get("remote"))
                location = clean_text(item.get("location")) or ("Remote" if remote else "Unspecified")
                posting = self._build(
                    id=JobPosting.create_id(url, company, title),
                    title=title,
                    company=company,
                    location=location,
                    job_url=url,
                    apply_url=url,
                    description=description,
                    date_posted=clean_text(item.get("created_at")) or None,
                    is_remote=remote,
                    source="arbeitnow",
                    contacts=job_post_contacts(description),
                )
                if posting:
                    jobs.append(posting)
                if len(jobs) >= params.max_results_per_board:
                    break
            links = data.get("links") or {}
            if not rows or not links.get("next"):
                break
            page += 1
        return jobs

    def fetch_jobicy(self, params: SearchParameters) -> List[JobPosting]:
        """Documented public feed; preserve original URLs and geographic restrictions."""
        check_cancelled()
        data = self._get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": 200})
        if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
            self.errors.append("Jobicy did not return a jobs list")
            return []
        jobs = []
        for item in data.get("jobs") or []:
            if not isinstance(item, dict):
                continue
            title = strip_html(item.get("jobTitle", ""))
            if not title_matches(title, params.target_domains):
                continue
            description = strip_html(item.get("jobDescription", ""))
            job = self._build(
                id=JobPosting.create_id(item.get("url", ""), item.get("companyName", ""), title),
                title=title, company=strip_html(item.get("companyName", "")),
                job_url=item.get("url"), apply_url=item.get("url"),
                location=strip_html(item.get("jobGeo", "")) or "Remote",
                date_posted=item.get("pubDate"), description=description,
                is_remote=True, work_mode="remote", source="jobicy",
                # Do not compare monthly/hourly salary bands against an annual floor.
                salary_min=item.get("annualSalaryMin") if item.get("salaryPeriod") == "year" else None,
                salary_max=item.get("annualSalaryMax") if item.get("salaryPeriod") == "year" else None,
                salary_currency=item.get("salaryCurrency"), contacts=job_post_contacts(description),
            )
            if job:
                jobs.append(job)
            if len(jobs) >= params.max_results_per_board:
                break
        return jobs

    def scrape_configured(self, params: SearchParameters) -> List[JobPosting]:
        fetchers = {"remotive": self.fetch_remotive, "arbeitnow": self.fetch_arbeitnow, "jobicy": self.fetch_jobicy}
        self.report = {}
        jobs: List[JobPosting] = []
        for source in params.public_sources:
            check_cancelled()
            self.errors = []
            self.cached_requests = 0
            try:
                found = fetchers[source](params)
                console.print(f"[green]{source.title()} provided {len(found)} matching listings[/green]")
                jobs.extend(found)
                self.report[source] = {"status": "partial" if self.errors and found else "failed" if self.errors else "ok",
                                       "matching_listings": len(found), "errors": list(self.errors),
                                       "cached_requests": self.cached_requests}
            except RunCancelled:
                raise
            except Exception as exc:
                self.report[source] = {"status": "failed", "matching_listings": 0, "errors": [str(exc)[:160]]}
                console.print(f"[yellow]{source.title()} feed issue: {str(exc)[:160]}[/yellow]")
        return jobs
