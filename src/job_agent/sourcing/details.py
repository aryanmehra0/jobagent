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
    """The most reliable candidate wins, not the longest.

    Priority order: structured JSON-LD (most reliable), then a targeted
    selector match, then the whole page body as a last resort. Sorting every
    tier together by raw length let generic page chrome (nav bars, "people
    also viewed" lists, footer/legal text) outscore a short, accurate,
    structured description just by being longer.
    """
    soup = BeautifulSoup(html, "html.parser")

    jsonld = _jsonld_descriptions(soup)
    if jsonld:
        return clean_text(max(jsonld, key=len))[:20000]

    selector_candidates: list[str] = []
    for selector in DETAIL_SELECTORS:
        for node in soup.select(selector):
            text = strip_html(str(node))
            if len(text) >= DESCRIPTION_MIN_CHARS:
                selector_candidates.append(text)
    if selector_candidates:
        return clean_text(max(selector_candidates, key=len))[:20000]

    body = soup.body
    if body:
        for noisy in body.select("nav, header, footer, script, style, noscript, svg, form"):
            noisy.decompose()
        text = strip_html(str(body))
        if len(text) >= DESCRIPTION_MIN_CHARS:
            return clean_text(text)[:20000]
    return ""


from job_agent.netguard import resolves_to_public_address as _resolves_to_public_address  # noqa: E402
from job_agent.netguard import safe_get  # noqa: E402


def _fetch_description(job: JobPosting, session, proxy: str | None = None) -> str:
    """Fetch and extract a job's description, refusing anything not a public host.

    Job URLs come from third-party scrape/feed data, not user input, but this
    is still the only place in the codebase that fetches arbitrary
    caller-supplied URLs for non-LinkedIn sources, so it gets the same
    resolves-to-a-public-address check used for employer site crawling.
    """
    parsed = urlsplit(job.job_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not _resolves_to_public_address(parsed.hostname, port):
        return ""

    # Every redirect hop is re-checked: a public page must not be able to bounce us to an internal one.
    response = safe_get(
        session, job.job_url,
        check=lambda host, port: _resolves_to_public_address(host, port),
        timeout=10,
        stream=True,
        proxies={"http": proxy, "https": proxy} if proxy else None,
        headers={"User-Agent": "Mozilla/5.0 JobAgent/0.2 (+local job search assistant)"},
    )
    if response is None:
        return ""
    with response:
        response.raise_for_status()
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type and not any(part in content_type for part in ("html", "text", "json")):
            return ""
        # One bounded read and one decode, using the server-declared (or
        # sniffed) encoding, instead of iterating decoded chunks: chunking a
        # str stream can split a multi-byte character across chunk
        # boundaries, and re-encoding each chunk just to count its bytes was
        # pure overhead.
        raw = response.raw.read(MAX_DETAIL_BYTES, decode_content=True)
        html = raw.decode(response.encoding or response.apparent_encoding or "utf-8", errors="replace")
    return extract_description(html)


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


def _can_fetch(job: JobPosting) -> bool:
    """Whether a listing's page may be fetched for its description (LinkedIn needs a real view URL)."""
    parsed = urlsplit(job.job_url)
    if job.source == "linkedin":
        return (parsed.hostname or "").endswith(".linkedin.com") and bool(re.fullmatch(r"/jobs/view/\d+/?", parsed.path))
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def enrich_missing_descriptions(jobs: list[JobPosting], *, workers: int = 4, proxy: str | None = None,
                                minimum_chars: int = DESCRIPTION_MIN_CHARS):
    """Fetch a description for every listing that came without a usable one.

    Listing pages are network-bound, so a few are fetched at a time; one at a
    time, a hundred listings with a ten-second timeout each is a quarter of an
    hour of waiting. Same safety as `enrich_job_details` (public hosts only,
    bounded reads). Returns (jobs, report).
    """
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    report = {"requested": 0, "fetched": 0, "unavailable": 0}
    targets = [i for i, job in enumerate(jobs) if _thin(job.description, minimum_chars)]
    targets = [i for i in targets if _can_fetch(jobs[i])]
    report["unavailable"] += sum(1 for job in jobs if _thin(job.description, minimum_chars)) - len(targets)
    if not targets:
        return jobs, report
    report["requested"] = len(targets)

    local = threading.local()

    def fetch(index: int) -> tuple[int, str]:
        if not hasattr(local, "session"):
            local.session = requests.Session()
        try:
            return index, _fetch_description(jobs[index], local.session, proxy=proxy)
        except (requests.RequestException, ValueError):
            return index, ""

    result = list(jobs)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [pool.submit(fetch, index) for index in targets]
        for done in as_completed(futures):
            check_cancelled()
            index, description = done.result()
            if not description:
                report["unavailable"] += 1
                continue
            job = result[index]
            result[index] = JobPosting.model_validate({
                **job.model_dump(), "description": description,
                "contacts": [c.model_dump() for c in job.contacts] + job_post_contacts(description)})
            report["fetched"] += 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return result, report


def enrich_linkedin_details(jobs: list[JobPosting], session=None, proxy: str | None = None):
    return enrich_job_details(jobs, session=session, proxy=proxy)
