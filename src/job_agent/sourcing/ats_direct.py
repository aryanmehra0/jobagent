"""Direct ATS endpoint ingestion.

Queries the public career APIs of Greenhouse, Lever, and Ashby directly. These
feeds return the full, unabridged job description and carry no aggregator rate
limits, which makes them both cheaper and higher quality than scraping a board.

Relevance filtering here is token-based rather than substring-based: a target
domain of "Senior Distributed Systems Engineer" matches a posting titled
"Distributed Systems Engineer II", which a whole-phrase `in` test never would.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from rich.console import Console

from job_agent.config.normalize import clean_text, parse_posting_timestamp, strip_html, tokenize
from job_agent.config.schema import JobPosting, SearchParameters
from job_agent.contacts.extract import job_post_contacts

console = Console()

# Default registry, used when `searches.yaml` does not define `ats_companies`.
DEFAULT_ATS_REGISTRY: Dict[str, List[str]] = {
    "greenhouse": ["stripe", "figma", "dropbox", "databricks"],
    "lever": ["netflix", "twitch"],
    "ashby": ["notion", "linear"],
}

# Words that appear in nearly every engineering title and so carry no signal.
_STOPWORD_TOKENS = {
    "senior", "staff", "principal", "lead", "junior", "mid", "level", "the", "and",
    "for", "with", "engineer", "engineering", "developer", "manager", "specialist", "ii", "iii",
}

# A posting must share at least this fraction of a target domain's meaningful tokens.
MIN_TOKEN_OVERLAP_RATIO = 0.5


class ATSDirectIngestion:
    """Ingests job listings directly from corporate ATS feeds."""

    def __init__(self, timeout: int = 15, session: Optional[requests.Session] = None):
        self.timeout = timeout
        self.session = session or requests.Session()
        self.report: Dict[str, Any] = {}
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
        })

    # --- Provider adapters ----------------------------------------------------

    def _get_json(self, url: str, label: str) -> Optional[Any]:
        """GET a JSON endpoint, returning None and logging on any failure."""
        try:
            response = self.session.get(url, timeout=self.timeout)
        except requests.RequestException as exc:
            self.report[label] = {"status": "failed", "detail": exc.__class__.__name__}
            console.print(f"[dim]{label}: request failed ({exc.__class__.__name__}).[/dim]")
            return None
        if response.status_code == 404:
            self.report[label] = {"status": "failed", "detail": "HTTP 404; board token not found"}
            console.print(f"[dim]{label}: board not found (check the token in searches.yaml).[/dim]")
            return None
        if response.status_code != 200:
            self.report[label] = {"status": "failed", "detail": f"HTTP {response.status_code}"}
            console.print(f"[dim]{label}: HTTP {response.status_code}.[/dim]")
            return None
        try:
            data = response.json()
            self.report[label] = {"status": "ok"}
            return data
        except ValueError:
            self.report[label] = {"status": "failed", "detail": "Invalid JSON"}
            console.print(f"[dim]{label}: response was not JSON.[/dim]")
            return None

    def _build(self, **kwargs: Any) -> Optional[JobPosting]:
        """Construct a validated posting, returning None when validation rejects it."""
        try:
            return JobPosting(**kwargs)
        except Exception as exc:
            console.print(f"[dim]Skipped ATS listing ({kwargs.get('title', '?')}): {exc}[/dim]")
            return None

    def fetch_greenhouse_jobs(self, board_token: str) -> List[JobPosting]:
        """Fetch jobs from the Greenhouse public board API."""
        data = self._get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true",
            f"Greenhouse/{board_token}",
        )
        if not isinstance(data, dict):
            return []

        # The board metadata carries the real company name; the token is a slug.
        company = clean_text((data.get("meta") or {}).get("name", "")) or board_token.replace("-", " ").title()

        postings: List[JobPosting] = []
        for item in data.get("jobs", []):
            job_url = clean_text(item.get("absolute_url"))
            title = clean_text(item.get("title"))
            if not job_url or not title:
                continue
            location = clean_text((item.get("location") or {}).get("name")) or "Remote"
            description = strip_html(item.get("content", ""))
            # The hosted page redirects to custom careers sites for some companies;
            # the embeddable form is always the application itself.
            apply_url = (
                f"https://job-boards.greenhouse.io/embed/job_app?for={board_token}&token={item['id']}"
                if item.get("id") else None
            )
            posting = self._build(
                id=JobPosting.create_id(job_url, company, title),
                title=title,
                company=company,
                location=location,
                job_url=job_url,
                description=description,
                # `updated_at` changes whenever a recruiter edits an old posting;
                # `first_published` is when it went live.
                date_posted=clean_text(item.get("first_published") or item.get("updated_at")) or None,
                is_remote="remote" in location.lower(),
                source="greenhouse",
                apply_url=apply_url,
                contacts=job_post_contacts(description),
            )
            if posting:
                postings.append(posting)
        return postings

    def fetch_lever_jobs(self, company_token: str) -> List[JobPosting]:
        """Fetch jobs from the Lever public postings API."""
        data = self._get_json(
            f"https://api.lever.co/v0/postings/{company_token}?mode=json",
            f"Lever/{company_token}",
        )
        if not isinstance(data, list):
            return []

        company = company_token.replace("-", " ").title()
        postings: List[JobPosting] = []
        for item in data:
            job_url = clean_text(item.get("hostedUrl") or item.get("applyUrl"))
            title = clean_text(item.get("text"))
            if not job_url or not title:
                continue
            categories = item.get("categories") or {}
            location = clean_text(categories.get("location")) or "Remote"
            workplace_type = clean_text(item.get("workplaceType")).lower()
            description = clean_text(item.get("descriptionPlain")) or strip_html(item.get("description", ""))
            apply_url = clean_text(item.get("applyUrl")) or (f"{job_url.rstrip('/')}/apply" if item.get("hostedUrl") else None)
            posting = self._build(
                id=JobPosting.create_id(job_url, company, title),
                title=title,
                company=company,
                location=location,
                job_url=job_url,
                description=description,
                date_posted=str(item.get("createdAt")) if item.get("createdAt") else None,
                is_remote="remote" in location.lower() or workplace_type == "remote",
                work_mode=(workplace_type if workplace_type in {"remote", "hybrid"} else None),
                job_type=clean_text(categories.get("commitment")) or None,
                source="lever",
                apply_url=apply_url,
                contacts=job_post_contacts(description),
            )
            if posting:
                postings.append(posting)
        return postings

    def fetch_ashby_jobs(self, organization: str) -> List[JobPosting]:
        """Fetch jobs from the Ashby public job board API."""
        data = self._get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{organization}",
            f"Ashby/{organization}",
        )
        if not isinstance(data, dict):
            return []

        company = organization.replace("-", " ").title()
        postings: List[JobPosting] = []
        for item in data.get("jobs", []):
            job_url = clean_text(item.get("jobUrl") or item.get("applyUrl"))
            title = clean_text(item.get("title"))
            if not job_url or not title:
                continue
            location = clean_text(item.get("location")) or "Remote"
            description = strip_html(item.get("descriptionHtml", "")) or clean_text(item.get("descriptionPlain"))
            apply_url = clean_text(item.get("applyUrl")) or (f"{job_url.rstrip('/')}/application" if item.get("jobUrl") else None)
            posting = self._build(
                id=JobPosting.create_id(job_url, company, title),
                title=title,
                company=company,
                location=location,
                job_url=job_url,
                description=description,
                date_posted=clean_text(item.get("publishedAt")) or None,
                is_remote=bool(item.get("isRemote")) or "remote" in location.lower(),
                work_mode=(
                    clean_text(item.get("workplaceType")).lower()
                    if clean_text(item.get("workplaceType")).lower() in {"remote", "hybrid", "onsite"}
                    else None
                ),
                job_type=clean_text(item.get("employmentType")) or None,
                source="ashby",
                apply_url=apply_url,
                contacts=job_post_contacts(description),
            )
            if posting:
                postings.append(posting)
        return postings

    # --- Relevance filtering --------------------------------------------------

    @staticmethod
    def _domain_tokens(domain: str) -> set:
        """Meaningful tokens of a target domain, with generic title words removed."""
        return {token for token in tokenize(domain) if token not in _STOPWORD_TOKENS}

    @staticmethod
    def _required_title_tokens(wanted: set) -> int:
        """How many of a domain's tokens a title must contain to count as a match.

        At least two whenever the domain offers two, because a bare ratio lets one
        generic word carry a match: "Product Designer" reduces to
        {product, designer}, and a 50% ratio matched "Account Executive, Product"
        on the word "product" alone.
        """
        if len(wanted) <= 1:
            return 1
        return max(2, -(-len(wanted) * 10 // 20))  # ceil(len * MIN_TOKEN_OVERLAP_RATIO)

    def matches_domain(self, posting: JobPosting, domains: List[str]) -> bool:
        """Whether a posting is relevant to any target domain.

        The title is the primary signal: a posting matches when it shares at least
        half of the domain's meaningful tokens.

        The description is a deliberately narrow secondary signal - the domain must
        appear in the opening of the posting as a contiguous phrase. Scattered
        token matching over the whole body is far too loose: "Machine Learning
        Engineer" reduces to {machine, learning}, and both words appear somewhere
        in almost every large company's boilerplate, which matched sales roles.
        Borderline postings are better rejected here and recovered by the semantic
        evaluation in Phase 3, which judges relevance properly.
        """
        title_tokens = tokenize(posting.title)
        # Only the opening of the posting, which is the role summary rather than
        # the benefits, legal, and "about us" sections.
        summary = posting.description[:1200].lower()

        for domain in domains:
            wanted = self._domain_tokens(domain)
            if not wanted:
                # The domain was nothing but generic title words ("Engineer");
                # fall back to matching its raw tokens against the title.
                if tokenize(domain) & title_tokens:
                    return True
                continue

            if len(wanted & title_tokens) >= self._required_title_tokens(wanted):
                return True

            # `tokenize` returns a set, so rebuild the phrase in document order.
            ordered = [token for token in re.findall(r"[a-z0-9+#.]+", domain.lower()) if token in wanted]
            phrase = " ".join(ordered)
            if len(wanted) >= 2 and phrase and phrase in summary:
                return True
        return False

    def _is_fresh(self, posting: JobPosting, hours_old: int) -> bool:
        """Whether a posting falls inside the freshness window.

        A posting with no parseable date is kept: ATS feeds often omit the field,
        and discarding them would silently lose the highest-quality source.
        """
        posted = parse_posting_timestamp(posting.date_posted)
        if posted is None:
            return True
        return posted >= datetime.now(timezone.utc) - timedelta(hours=hours_old)

    # --- Orchestration --------------------------------------------------------

    def scrape_configured_ats(
        self,
        companies: Optional[Dict[str, List[str]]] = None,
        search_params: Optional[SearchParameters] = None,
    ) -> List[JobPosting]:
        """Poll every configured ATS board and return the postings that match the search."""
        registry = DEFAULT_ATS_REGISTRY if companies is None else companies
        self.report = {}
        fetchers = {
            "greenhouse": self.fetch_greenhouse_jobs,
            "lever": self.fetch_lever_jobs,
            "ashby": self.fetch_ashby_jobs,
        }

        all_postings: List[JobPosting] = []
        for provider, tokens in registry.items():
            fetcher = fetchers.get(provider)
            if not fetcher:
                console.print(f"[dim]Unknown ATS provider '{provider}' ignored.[/dim]")
                continue
            for token in tokens:
                from job_agent.runtime import check_cancelled, RunCancelled

                check_cancelled()
                try:
                    all_postings.extend(fetcher(token))
                except RunCancelled:
                    raise
                except Exception as exc:
                    self.report[f"{provider}/{token}"] = {"status": "failed", "detail": str(exc)[:160]}
                    console.print(f"[dim]{provider}/{token}: {exc.__class__.__name__}; continuing other boards.[/dim]")

        if not search_params:
            return all_postings

        filtered: List[JobPosting] = []
        for posting in all_postings:
            if not self.matches_domain(posting, search_params.target_domains):
                continue
            if not self._is_fresh(posting, search_params.hours_old):
                continue
            filtered.append(posting)

        console.print(
            f"[dim]ATS feeds: {len(all_postings)} postings fetched, {len(filtered)} matched the search.[/dim]"
        )
        return filtered
