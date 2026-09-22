"""Fetch missing descriptions only after title filtering and deduplication."""
from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from job_agent.config.normalize import clean_text, strip_html
from job_agent.config.schema import JobPosting
from job_agent.contacts.extract import job_post_contacts
from job_agent.runtime import check_cancelled


DESCRIPTION_MIN_CHARS = 80
MAX_DETAIL_BYTES = 1_000_000
DETAIL_SELECTORS = (
    ".show-more-less-html__markup",
    "[data-automation-id='jobPostingDescription']",
    "[data-qa='job-description']",
    "[data-testid='jobDescriptionText']",
    ".job-description",
    ".jobDescription",
    "#jobDescriptionText",
    "article",
    "main",
)


def _thin(description: str | None, minimum: int = DESCRIPTION_MIN_CHARS) -> bool:
    return len(clean_text(description or "")) < minimum


def _jsonld_descriptions(soup: BeautifulSoup) -> list[str]:
    descriptions = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(script.string or "")
        except ValueError:
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
                continue
            if not isinstance(item, dict):
                continue
            kind = item.get("@type")
            if isinstance(kind, list):
                is_job = any(str(value).lower() == "jobposting" for value in kind)
            else:
                is_job = str(kind).lower() == "jobposting"
            description = strip_html(str(item.get("description") or ""))
            if is_job and len(description) >= DESCRIPTION_MIN_CHARS:
                descriptions.append(description)
            for key in ("@graph", "itemListElement"):
                child = item.get(key)
                if isinstance(child, (list, dict)):
                    stack.append(child)
    return descriptions


def extract_description(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    candidates = _jsonld_descriptions(soup)
    for selector in DETAIL_SELECTORS:
        for node in soup.select(selector):
            text = strip_html(str(node))
            if len(text) >= DESCRIPTION_MIN_CHARS:
                candidates.append(text)
    body = soup.body
    if body:
        for noisy in body.select("nav, header, footer, script, style, noscript, svg, form"):
            noisy.decompose()
        text = strip_html(str(body))
        if len(text) >= DESCRIPTION_MIN_CHARS:
            candidates.append(text)
    if not candidates:
        return ""
    candidates.sort(key=len, reverse=True)
    return clean_text(candidates[0])[:20000]


def _fetch_description(job: JobPosting, session, proxy: str | None = None) -> str:
    parsed = urlsplit(job.job_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    response = session.get(
        job.job_url,
        timeout=10,
        stream=True,
        proxies={"http": proxy, "https": proxy} if proxy else None,
        headers={"User-Agent": "Mozilla/5.0 JobAgent/0.2 (+local job search assistant)"},
    )
    response.raise_for_status()
    content_type = (response.headers.get("content-type") or "").lower()
    if content_type and not any(part in content_type for part in ("html", "text", "json")):
        return ""
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=65536, decode_unicode=True):
        if not chunk:
            continue
        total += len(chunk.encode("utf-8", errors="ignore") if isinstance(chunk, str) else chunk)
        if total > MAX_DETAIL_BYTES:
            break
        chunks.append(chunk.decode("utf-8", errors="ignore") if isinstance(chunk, bytes) else chunk)
    return extract_description("".join(chunks))


def enrich_job_details(jobs: list[JobPosting], session=None, proxy: str | None = None, minimum_chars: int = DESCRIPTION_MIN_CHARS):
    session = session or requests.Session()
    report = {"requested": 0, "fetched": 0, "unavailable": 0, "skipped_complete": 0}
    result = []
    for job in jobs:
        check_cancelled()
        if not _thin(job.description, minimum_chars):
            result.append(job)
            report["skipped_complete"] += 1
            continue
        parsed = urlsplit(job.job_url)
        if job.source == "linkedin" and (
            not (parsed.hostname or "").endswith(".linkedin.com") or not re.fullmatch(r"/jobs/view/\d+/?", parsed.path)
        ):
            result.append(job)
            report["unavailable"] += 1
            continue
        report["requested"] += 1
        try:
            description = _fetch_description(job, session, proxy=proxy)
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


def enrich_linkedin_details(jobs: list[JobPosting], session=None, proxy: str | None = None):
    return enrich_job_details(jobs, session=session, proxy=proxy)
