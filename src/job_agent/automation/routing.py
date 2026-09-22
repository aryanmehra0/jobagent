"""Decide where, and whether, a job can be applied to automatically.

A job board listing is not an application form. LinkedIn and Indeed pages sit
behind a login, and automating those logins breaks the sites' terms and risks
the candidate's account. The applicant tracking systems employers use —
Greenhouse, Lever, Ashby — publish their forms openly, at addresses that can be
derived from the listing. Every form URL pattern here was verified against a
live posting before being added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

from job_agent.config.schema import JobPosting

# Hosts whose job pages require the candidate to be signed in.
LOGIN_REQUIRED_DOMAINS = (
    "linkedin.com", "indeed.com", "glassdoor.com", "glassdoor.co.in", "ziprecruiter.com",
    "naukri.com", "monster.com", "foundit.in", "instahyre.com", "wellfound.com",
)

# Employer application systems that make the candidate create an account (with
# email verification) before the form appears. Creating accounts on a person's
# behalf is not something to automate.
ACCOUNT_REQUIRED_DOMAINS = (
    "myworkdayjobs.com", "myworkdaysite.com", "workday.com", "successfactors.com", "successfactors.eu",
    "taleo.net", "oraclecloud.com", "amazon.jobs", "brassring.com", "kenexa.com", "avature.net",
)

AUTO_APPLY_CHANNELS = ("greenhouse", "lever", "ashby")


@dataclass(frozen=True)
class ApplicationRoute:
    """Where to send the browser, and whether automation should try."""

    channel: str          # greenhouse | lever | ashby | employer_site | account_required | login_required | unknown
    url: Optional[str]
    automatable: bool
    reason: str


def _host(url: Optional[str]) -> str:
    return (urlparse(url).netloc or "").lower() if url else ""


def _on_domain(url: Optional[str], domains) -> bool:
    host = _host(url)
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _requires_login(url: Optional[str]) -> bool:
    return _on_domain(url, LOGIN_REQUIRED_DOMAINS)


def ats_form_url(url: Optional[str]) -> Optional[tuple]:
    """Map a known ATS listing URL to its public application form.

    Returns `(channel, form_url)`, or None when the URL is not a recognised ATS
    listing.
    """
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.rstrip("/")

    # Greenhouse: the embeddable form works even for companies that redirect
    # their hosted board page to a custom careers site.
    if host.endswith("greenhouse.io"):
        query = parse_qs(parsed.query)
        if "embed/job_app" in path and query.get("for") and query.get("token"):
            return "greenhouse", url
        match = re.match(r"^/([^/]+)/jobs/(\d+)", path)
        if match:
            token, job_id = match.groups()
            return "greenhouse", f"https://job-boards.greenhouse.io/embed/job_app?for={token}&token={job_id}"

    if host == "jobs.lever.co":
        match = re.match(r"^/([^/]+)/([0-9a-f-]{36})(?:/apply)?$", path)
        if match:
            company, job_id = match.groups()
            return "lever", f"https://jobs.lever.co/{company}/{job_id}/apply"

    if host == "jobs.ashbyhq.com":
        match = re.match(r"^/([^/]+)/([0-9a-f-]{36})(?:/application)?$", path)
        if match:
            organization, job_id = match.groups()
            return "ashby", f"https://jobs.ashbyhq.com/{organization}/{job_id}/application"

    return None


def greenhouse_form_url(board_token: str, job_id) -> str:
    """The embeddable Greenhouse application form for a board's job."""
    return f"https://job-boards.greenhouse.io/embed/job_app?for={board_token}&token={job_id}"


def resolve_apply_url(job_url: Optional[str], direct_url: Optional[str]) -> Optional[str]:
    """The best application address known for a board listing.

    Prefers an ATS form derived from either URL, then the employer's own page
    (JobSpy's `job_url_direct`), and returns None when only a login-walled board
    page is available.
    """
    for candidate in (direct_url, job_url):
        form = ats_form_url(candidate)
        if form:
            return form[1]
    if direct_url and not _requires_login(direct_url):
        return direct_url
    return None


def is_workday(url: Optional[str]) -> bool:
    return _on_domain(url, ("myworkdayjobs.com", "myworkdaysite.com"))


def route_application(job: JobPosting, *, assist_workday: bool = False) -> ApplicationRoute:
    """Choose how to apply to a job, and say plainly when automation cannot."""
    for candidate in (job.apply_url, job.job_url):
        if is_workday(candidate):
            return ApplicationRoute("workday_assisted" if assist_workday else "account_required", candidate,
                                    assist_workday, "Workday requires user-assisted account access and final review. Automatic submission is disabled.")
    for candidate in (job.apply_url, job.job_url):
        form = ats_form_url(candidate)
        if form:
            channel, url = form
            return ApplicationRoute(channel, url, True, f"Public {channel.title()} application form.")

    if job.apply_url and _on_domain(job.apply_url, ACCOUNT_REQUIRED_DOMAINS):
        return ApplicationRoute(
            "account_required", job.apply_url, False,
            "The employer's application system requires creating an account. Apply manually from the apply link.",
        )

    if job.apply_url and not _requires_login(job.apply_url):
        return ApplicationRoute(
            "employer_site", job.apply_url, True,
            "Employer's own careers page; auto-apply will try, and hands off if no form is found.",
        )

    if _requires_login(job.job_url):
        host = _host(job.job_url).replace("www.", "")
        return ApplicationRoute(
            "login_required", job.job_url, False,
            f"{host} requires signing in to apply. Apply manually from the job link.",
        )

    return ApplicationRoute("unknown", job.job_url, True, "Unrecognised site; auto-apply will try.")
