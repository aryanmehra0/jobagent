"""Fetch missing descriptions only after title filtering and deduplication."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from job_agent.config.normalize import strip_html
from job_agent.config.schema import JobPosting
from job_agent.contacts.extract import job_post_contacts
from job_agent.runtime import check_cancelled


def enrich_linkedin_details(jobs: list[JobPosting], session=None, proxy: str | None = None):
    session = session or requests.Session()
    report = {"requested": 0, "fetched": 0, "unavailable": 0}
    result = []
    for job in jobs:
        check_cancelled()
        if job.source != "linkedin" or job.description:
            result.append(job)
            continue
        parsed = urlsplit(job.job_url)
        if not (parsed.hostname or "").endswith(".linkedin.com") or not re.fullmatch(r"/jobs/view/\d+/?", parsed.path):
            result.append(job)
            report["unavailable"] += 1
            continue
        report["requested"] += 1
        try:
            response = session.get(job.job_url, timeout=10,
                                   proxies={"http": proxy, "https": proxy} if proxy else None)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            content = soup.select_one(".show-more-less-html__markup")
            description = strip_html(str(content)) if content else ""
            if description:
                job = JobPosting.model_validate({**job.model_dump(), "description": description,
                    "contacts": [c.model_dump() for c in job.contacts] + job_post_contacts(description)})
                report["fetched"] += 1
            else:
                report["unavailable"] += 1
        except (requests.RequestException, ValueError):
            report["unavailable"] += 1
        result.append(job)
    return result, report
