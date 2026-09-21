"""Find published contact emails for sourced jobs.

Two sources, both limited to addresses a company has chosen to publish:

* The company's own website: the homepage plus its careers, jobs, contact and
  about pages. robots.txt is honoured, and only addresses on the company's own
  domain are kept, so a crawled page quoting a partner or a customer does not
  attribute that address to the employer.
* Hunter.io, when `HUNTER_API_KEY` is set: its domain search, restricted to
  role mailboxes (careers@, hr@, jobs@). Named individuals are not requested.

Nothing is ever guessed. An address pattern such as firstname.lastname@ is not
constructed, because a wrong guess reaches a stranger or bounces, and a right
one is still an unsolicited message to a person who never published it.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from rich.console import Console

from job_agent.config.schema import JobPosting
from job_agent.contacts.extract import extract_emails, registrable_domain
from job_agent.runtime import check_cancelled

console = Console()

USER_AGENT = "Mozilla/5.0 (compatible; job-agent/0.2; +personal job search)"

# Where companies publish recruiting and contact addresses, most useful first.
CANDIDATE_PATHS = (
    "/careers", "/jobs", "/contact", "/contact-us", "/about", "/about-us", "/company/careers",
)
# Link text or paths on the homepage that lead to the same kinds of pages.
_LINK_HINTS = re.compile(r"career|jobs|join|hiring|work-with-us|contact|about", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.IGNORECASE)

# Hosts that belong to job boards or applicant tracking systems rather than to
# the employer; their addresses are the platform's, not the company's.
PLATFORM_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.co.in", "ziprecruiter.com", "naukri.com",
    "greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "workday.com", "smartrecruiters.com",
    "icims.com", "jobvite.com", "bamboohr.com", "workable.com", "breezy.hr", "recruitee.com",
    "freshteam.com", "keka.com", "darwinbox.in", "zohorecruit.com", "google.com", "facebook.com",
}

MAX_PAGES_PER_SITE = 6
MAX_PAGE_BYTES = 1_500_000


def employer_website(job: JobPosting) -> Optional[str]:
    """The employer's own site for a job, or None when only platforms are known."""
    for candidate in (job.company_website, job.apply_url):
        if not candidate:
            continue
        parsed = urlparse(candidate)
        domain = registrable_domain(parsed.netloc)
        if domain and domain not in PLATFORM_DOMAINS:
            return f"{parsed.scheme or 'https'}://{parsed.netloc}"
    return None


class CompanySiteCrawler:
    """Reads a company's public pages for the email addresses it publishes."""

    def __init__(self, session: Optional[requests.Session] = None, timeout: float = 8.0,
                 max_pages: int = MAX_PAGES_PER_SITE):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)
        self.timeout = timeout
        self.max_pages = max_pages
        # One crawl per company domain per run, however many jobs it posted.
        self._cache: Dict[str, List[dict]] = {}

    def _get(self, url: str) -> Optional[str]:
        try:
            response = self.session.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
            content_type = response.headers.get("Content-Type", "")
            if response.status_code != 200 or ("html" not in content_type and "text" not in content_type):
                response.close()
                return None
            body = response.raw.read(MAX_PAGE_BYTES, decode_content=True)
            response.close()
            return body.decode(response.encoding or "utf-8", errors="replace")
        except Exception:
            return None

    def _robots(self, root: str) -> RobotFileParser:
        parser = RobotFileParser()
        text = self._get(urljoin(root, "/robots.txt"))
        parser.parse((text or "").splitlines())
        return parser

    def _candidate_urls(self, root: str, homepage: Optional[str]) -> List[str]:
        """Contact-like pages linked from the homepage, then the conventional paths."""
        domain = registrable_domain(urlparse(root).netloc)
        urls: List[str] = []
        for href in _HREF_RE.findall(homepage or ""):
            absolute = urljoin(root + "/", href.strip())
            parsed = urlparse(absolute)
            if parsed.scheme not in ("http", "https") or registrable_domain(parsed.netloc) != domain:
                continue
            if _LINK_HINTS.search(parsed.path):
                urls.append(absolute.split("?")[0])
        urls.extend(urljoin(root, path) for path in CANDIDATE_PATHS)
        seen, ordered = set(), []
        for url in urls:
            key = url.rstrip("/").lower()
            if key not in seen and key != root.rstrip("/").lower():
                seen.add(key)
                ordered.append(url)
        return ordered

    def find(self, website: str) -> List[dict]:
        """Published addresses on a company's own domain, as `JobContact` dicts."""
        parsed = urlparse(website if "://" in website else f"https://{website}")
        domain = registrable_domain(parsed.netloc)
        if not domain or domain in PLATFORM_DOMAINS:
            return []
        if domain in self._cache:
            return self._cache[domain]

        root = f"{parsed.scheme or 'https'}://{parsed.netloc}"
        robots = self._robots(root)
        found: Dict[str, dict] = {}

        def read(url: str) -> Optional[str]:
            if not robots.can_fetch(USER_AGENT, url):
                return None
            page = self._get(url)
            if page:
                for email, kind in extract_emails(page):
                    # Only the company's own addresses; subdomains such as
                    # careers.acme.com count as the company.
                    if (registrable_domain(email.split("@", 1)[1]) == domain and email not in found
                            and kind in ("hiring", "general")):
                        found[email] = {"email": email, "kind": kind, "source": "company_site", "source_url": url}
            return page

        homepage = read(root)
        for url in self._candidate_urls(root, homepage)[: self.max_pages - 1]:
            read(url)

        contacts = list(found.values())
        self._cache[domain] = contacts
        return contacts


class HunterClient:
    """Hunter.io domain search, restricted to role mailboxes."""

    ENDPOINT = "https://api.hunter.io/v2/domain-search"

    def __init__(self, api_key: str, session: Optional[requests.Session] = None, timeout: float = 10.0):
        self.api_key = api_key
        self.session = session or requests.Session()
        self.timeout = timeout
        self._cache: Dict[str, List[dict]] = {}
        self.disabled_reason: Optional[str] = None

    def find(self, domain: Optional[str] = None, company: Optional[str] = None) -> List[dict]:
        """Generic (role) addresses Hunter has found published for a company."""
        if self.disabled_reason or not (domain or company):
            return []
        key = (domain or company or "").lower()
        if key in self._cache:
            return self._cache[key]

        params = {"api_key": self.api_key, "type": "generic", "limit": 10}
        params.update({"domain": domain} if domain else {"company": company})
        try:
            response = self.session.get(self.ENDPOINT, params=params, timeout=self.timeout)
        except requests.RequestException:
            return []
        if response.status_code in (401, 403):
            self.disabled_reason = "Hunter.io rejected the API key."
        elif response.status_code == 429:
            self.disabled_reason = "Hunter.io quota or rate limit reached."
        if response.status_code != 200:
            if self.disabled_reason:
                console.print(f"[yellow]{self.disabled_reason} Skipping further lookups.[/yellow]")
            return []

        data = (response.json() or {}).get("data") or {}
        found_domain = data.get("domain") or domain
        contacts: List[dict] = []
        for item in data.get("emails") or []:
            email = (item.get("value") or "").lower()
            if not email or item.get("type") != "generic":
                continue
            # A company-name lookup can resolve to the wrong firm's domain; the
            # address must at least sit on the domain Hunter matched.
            if found_domain and registrable_domain(email.split("@", 1)[1]) != registrable_domain(found_domain):
                continue
            sources = item.get("sources") or []
            classified = extract_emails(email)
            contacts.append({
                "email": email,
                "kind": classified[0][1] if classified else "other",
                "source": "hunter",
                "source_url": (sources[0].get("uri") if sources else None),
                "confidence": item.get("confidence"),
            })
        self._cache[key] = contacts
        return contacts


def _merge(job: JobPosting, extra: Iterable[dict]) -> JobPosting:
    extra = list(extra)
    if not extra:
        return job
    contacts = [contact.model_dump() for contact in job.contacts] + extra
    # Re-validated so contacts are de-duplicated and ordered by usefulness.
    return JobPosting.model_validate({**job.model_dump(), "contacts": contacts})


def enrich_contacts(
    jobs: List[JobPosting],
    crawler: Optional[CompanySiteCrawler] = None,
    hunter: Optional[HunterClient] = None,
    workers: int = 4,
) -> Tuple[List[JobPosting], Dict[str, int]]:
    """Add company-site and Hunter contacts to each job.

    Jobs whose post already lists a hiring mailbox are not looked up further.
    Returns the updated jobs and counts for the sourcing summary.
    """
    crawler = crawler or CompanySiteCrawler()
    stats = {"with_email": 0, "from_post": 0, "from_site": 0, "from_hunter": 0}

    def lookup(job: JobPosting) -> List[dict]:
        if any(contact.kind == "hiring" for contact in job.contacts):
            return []
        found: List[dict] = []
        website = employer_website(job)
        if website:
            found.extend(crawler.find(website))
        if hunter and not any(item["kind"] == "hiring" for item in found):
            domain = registrable_domain(urlparse(website).netloc) if website else None
            found.extend(hunter.find(domain=domain, company=None if domain else job.company))
        return found

    enriched: List[JobPosting] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(lookup, job) for job in jobs]
        try:
            for job, future in zip(jobs, futures):
                check_cancelled()
                had_post = bool(job.contacts)
                extra = future.result()
                updated = _merge(job, extra)
                enriched.append(updated)
                stats["from_post"] += had_post
                stats["from_site"] += any(item["source"] == "company_site" for item in extra)
                stats["from_hunter"] += any(item["source"] == "hunter" for item in extra)
                stats["with_email"] += bool(updated.contacts)
        except BaseException:
            for future in futures:
                future.cancel()
            raise
    return enriched, stats
