"""Apply routing, contact capture during sourcing, contact lookup, and the jobs CSV.

Every network call here goes to an in-memory fake; nothing leaves the machine.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd
import pytest

from job_agent.automation.routing import ats_form_url, resolve_apply_url, route_application
from job_agent.config.schema import JobPosting
from job_agent.contacts.extract import job_post_contacts
from job_agent.contacts.finder import CompanySiteCrawler, HunterClient, employer_website, enrich_contacts

LEVER_ID = "0c9a6b8e-1d2f-4a5b-9c8d-7e6f5a4b3c2d"


def _job(**overrides) -> JobPosting:
    data = dict(id="j1", title="Product Manager", company="Acme",
                job_url="https://www.linkedin.com/jobs/view/1", source="linkedin")
    data.update(overrides)
    return JobPosting(**data)


# ==============================================================================
# APPLY ROUTING
# ==============================================================================

@pytest.mark.parametrize(
    "url,channel,form",
    [
        ("https://job-boards.greenhouse.io/stripe/jobs/7532733",
         "greenhouse", "https://job-boards.greenhouse.io/embed/job_app?for=stripe&token=7532733"),
        ("https://boards.greenhouse.io/figma/jobs/123", "greenhouse",
         "https://job-boards.greenhouse.io/embed/job_app?for=figma&token=123"),
        (f"https://jobs.lever.co/acme/{LEVER_ID}", "lever", f"https://jobs.lever.co/acme/{LEVER_ID}/apply"),
        (f"https://jobs.lever.co/acme/{LEVER_ID}/apply", "lever", f"https://jobs.lever.co/acme/{LEVER_ID}/apply"),
        (f"https://jobs.ashbyhq.com/notion/{LEVER_ID}", "ashby",
         f"https://jobs.ashbyhq.com/notion/{LEVER_ID}/application"),
    ],
)
def test_ats_listing_urls_map_to_their_application_forms(url, channel, form):
    assert ats_form_url(url) == (channel, form)


def test_login_walled_boards_are_routed_to_manual_apply():
    route = route_application(_job())
    assert route.channel == "login_required" and not route.automatable
    assert "linkedin.com" in route.reason


def test_employer_page_from_the_board_makes_a_board_job_automatable():
    job = _job(job_url="https://in.indeed.com/viewjob?jk=1", source="indeed",
               apply_url="https://careers.acme.com/jobs/42")
    route = route_application(job)
    assert route.automatable and route.url == "https://careers.acme.com/jobs/42"


def test_resolve_apply_url_prefers_an_ats_form_and_ignores_login_walls():
    assert resolve_apply_url("https://www.linkedin.com/jobs/view/1", None) is None
    assert resolve_apply_url("https://in.indeed.com/viewjob?jk=1", "https://www.indeed.com/applystart") is None
    assert resolve_apply_url(
        "https://in.indeed.com/viewjob?jk=1", f"https://jobs.lever.co/acme/{LEVER_ID}"
    ) == f"https://jobs.lever.co/acme/{LEVER_ID}/apply"


# ==============================================================================
# CAPTURE DURING SOURCING
# ==============================================================================

def test_jobspy_rows_keep_direct_url_company_site_and_published_emails():
    from job_agent.sourcing.scraper import OmnichannelScraper

    frame = pd.DataFrame([{
        "id": "in_1", "title": "Product Analyst", "company": "Acme", "location": "Mumbai",
        "job_url": "https://in.indeed.com/viewjob?jk=abc", "job_url_direct": "https://careers.acme.in/job/9",
        "company_url_direct": "https://acme.in", "emails": "talent@acme.in",
        "description": "Share your CV at hr [at] acme [dot] in", "site": "indeed",
    }])
    job = OmnichannelScraper()._normalize_jobspy_df(frame, default_source="indeed")[0]

    assert job.job_url == "https://in.indeed.com/viewjob?jk=abc"
    assert job.apply_url == "https://careers.acme.in/job/9"
    assert job.company_website == "https://acme.in"
    assert {contact.email for contact in job.contacts} == {"talent@acme.in", "hr@acme.in"}
    assert all(contact.source == "job_post" for contact in job.contacts)


def test_jobspy_email_list_values_are_accepted():
    assert [c["email"] for c in job_post_contacts("", ["careers@acme.com", None])] == ["careers@acme.com"]


class _FakeResponse:
    def __init__(self, status=200, payload=None, text="", content_type="text/html"):
        self.status_code = status
        self._payload = payload
        self.headers = {"Content-Type": content_type}
        self.encoding = "utf-8"
        body = text.encode("utf-8")

        class _Raw:
            def read(self, limit, decode_content=True):
                return body[:limit]

        self.raw = _Raw()

    def json(self):
        return self._payload

    def close(self):
        pass


class _FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.requested = []

    def get(self, url, params=None, **_):
        self.requested.append(url)
        handler = self.routes.get(url)
        if callable(handler):
            return handler(params)
        return handler or _FakeResponse(404)


def test_ats_feeds_record_the_real_application_form():
    from job_agent.sourcing.ats_direct import ATSDirectIngestion

    session = _FakeSession({
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true": _FakeResponse(payload={
            "meta": {"name": "Acme"},
            "jobs": [{"id": 55, "title": "PM", "absolute_url": "https://acme.com/careers?gh_jid=55",
                      "content": "&lt;p&gt;Questions? recruiting@acme.com&lt;/p&gt;", "location": {"name": "Remote"}}],
        }),
        "https://api.lever.co/v0/postings/acme?mode=json": _FakeResponse(payload=[{
            "text": "PM", "hostedUrl": f"https://jobs.lever.co/acme/{LEVER_ID}",
            "applyUrl": f"https://jobs.lever.co/acme/{LEVER_ID}/apply", "descriptionPlain": "Build things",
        }]),
    })
    feeder = ATSDirectIngestion(session=session)

    greenhouse = feeder.fetch_greenhouse_jobs("acme")[0]
    assert greenhouse.apply_url == "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=55"
    assert greenhouse.contacts[0].email == "recruiting@acme.com"
    assert route_application(greenhouse).channel == "greenhouse"

    lever = feeder.fetch_lever_jobs("acme")[0]
    assert lever.apply_url == f"https://jobs.lever.co/acme/{LEVER_ID}/apply"


# ==============================================================================
# COMPANY WEBSITE LOOKUP
# ==============================================================================

def _site(pages, robots=""):
    routes = {"https://acme.com/robots.txt": _FakeResponse(text=robots, content_type="text/plain")}
    routes.update({url: _FakeResponse(text=html) for url, html in pages.items()})
    return _FakeSession(routes)


def test_crawler_finds_the_company_addresses_and_records_where():
    session = _site({
        "https://acme.com": '<a href="/join-us">Careers</a> Office: info@acme.com',
        "https://acme.com/join-us": "Send resumes to <a href='mailto:careers@acme.com'>us</a>. "
                                    "Our agency partner: jobs@recruitfirm.com",
    })
    contacts = CompanySiteCrawler(session=session).find("https://acme.com/about")
    by_email = {c["email"]: c for c in contacts}

    assert "careers@acme.com" in by_email and "info@acme.com" in by_email
    assert by_email["careers@acme.com"]["source_url"] == "https://acme.com/join-us"
    # Another company's address on the page is not attributed to this employer.
    assert "jobs@recruitfirm.com" not in by_email


def test_crawler_honours_robots_txt():
    session = _site({"https://acme.com/careers": "careers@acme.com"}, robots="User-agent: *\nDisallow: /")
    assert CompanySiteCrawler(session=session).find("https://acme.com") == []
    assert "https://acme.com/careers" not in session.requested


def test_crawler_visits_each_company_once_per_run():
    session = _site({"https://acme.com": "hr@acme.com"})
    crawler = CompanySiteCrawler(session=session)
    crawler.find("https://acme.com")
    first = len(session.requested)
    crawler.find("https://careers.acme.com")
    assert len(session.requested) == first


def test_job_boards_and_ats_hosts_are_not_treated_as_the_employer_site():
    assert employer_website(_job(apply_url=f"https://jobs.lever.co/acme/{LEVER_ID}/apply")) is None
    assert employer_website(_job(company_website="https://acme.io")) == "https://acme.io"


# ==============================================================================
# HUNTER.IO
# ==============================================================================

def _hunter(payload, status=200):
    return _FakeSession({HunterClient.ENDPOINT: lambda params: _FakeResponse(status, payload)})


def test_hunter_returns_only_role_mailboxes_on_the_matched_domain():
    session = _hunter({"data": {"domain": "acme.com", "emails": [
        {"value": "careers@acme.com", "type": "generic", "confidence": 94, "sources": [{"uri": "https://acme.com/jobs"}]},
        {"value": "priya.sharma@acme.com", "type": "personal", "confidence": 90},
        {"value": "info@other.com", "type": "generic", "confidence": 80},
    ]}})
    contacts = HunterClient("key", session=session).find(company="Acme")
    assert [(c["email"], c["source"], c["confidence"]) for c in contacts] == [("careers@acme.com", "hunter", 94)]


def test_hunter_stops_after_a_rejected_key():
    session = _hunter({}, status=401)
    client = HunterClient("bad", session=session)
    assert client.find(domain="acme.com") == []
    assert client.find(domain="beta.com") == []
    assert len(session.requested) == 1


# ==============================================================================
# ENRICHMENT
# ==============================================================================

class _StubCrawler:
    def __init__(self, contacts):
        self.contacts = contacts
        self.calls = 0

    def find(self, website):
        self.calls += 1
        return self.contacts


def test_enrichment_adds_site_contacts_and_skips_jobs_that_already_list_a_hiring_email():
    crawler = _StubCrawler([{"email": "careers@acme.com", "kind": "hiring", "source": "company_site",
                             "source_url": "https://acme.com/careers"}])
    listed = _job(id="j2", company_website="https://acme.com",
                  contacts=[{"email": "hr@acme.com", "kind": "hiring", "source": "job_post"}])
    bare = _job(id="j3", company_website="https://acme.com")

    enriched, stats = enrich_contacts([listed, bare], crawler=crawler, workers=1)

    assert [c.email for c in enriched[0].contacts] == ["hr@acme.com"]
    assert enriched[1].primary_contact().email == "careers@acme.com"
    assert crawler.calls == 1 and stats["with_email"] == 2 and stats["from_site"] == 1


def test_enrichment_stops_when_the_user_presses_stop():
    import threading

    from job_agent.runtime import RunCancelled, cancellation

    event = threading.Event()
    event.set()
    with cancellation(event), pytest.raises(RunCancelled):
        enrich_contacts([_job(company_website="https://acme.com")], crawler=_StubCrawler([]), workers=1)


# ==============================================================================
# JOBS CSV
# ==============================================================================

def _rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["Job ID"]: row for row in csv.DictReader(handle)}


def test_csv_has_email_and_apply_columns_and_accumulates_across_sweeps(tmp_path):
    from job_agent.tracking.export import JobsCsvExporter

    outputs = tmp_path / "out"
    outputs.mkdir()
    first = _job(id="a1", company_website="https://acme.com", contacts=[
        {"email": "info@acme.com", "kind": "general", "source": "company_site", "source_url": "https://acme.com/contact"},
        {"email": "careers@acme.com", "kind": "hiring", "source": "job_post"},
    ])
    (outputs / "scraped_jobs.json").write_text(json.dumps([first.model_dump()]), encoding="utf-8")
    exporter = JobsCsvExporter(outputs_dir=outputs)
    exporter.export()

    row = _rows(exporter.csv_path)["a1"]
    assert row["HR / Careers Email"] == "careers@acme.com"
    assert row["Email Source"] == "Job post"
    assert row["Other Emails"] == "info@acme.com"
    assert row["Auto-apply Possible"].startswith("No")

    # A later sweep only writes its own jobs; earlier rows must survive.
    second = _job(id="b2", job_url=f"https://jobs.lever.co/beta/{LEVER_ID}", source="lever", company="Beta")
    (outputs / "scraped_jobs.json").write_text(json.dumps([second.model_dump()]), encoding="utf-8")
    exporter.export()

    rows = _rows(exporter.csv_path)
    assert set(rows) == {"a1", "b2"}
    assert rows["a1"]["HR / Careers Email"] == "careers@acme.com"
    assert rows["b2"]["Apply Method"] == "lever" and rows["b2"]["Auto-apply Possible"] == "Yes"


def test_csv_status_follows_the_pipeline_and_applied_is_never_undone(tmp_path):
    from job_agent.tracking.export import JobsCsvExporter

    outputs = tmp_path / "out"
    (outputs / "tailored_resumes").mkdir(parents=True)
    job = _job(id="c3")
    (outputs / "evaluated_jobs.json").write_text(
        json.dumps([{"job": job.model_dump(), "evaluation": {"fit_score": 8.25}}]), encoding="utf-8")
    (outputs / "qualified_jobs.json").write_text(
        json.dumps([{"job": job.model_dump(), "evaluation": {"fit_score": 8.25}}]), encoding="utf-8")
    exporter = JobsCsvExporter(outputs_dir=outputs)
    exporter.export()
    assert _rows(exporter.csv_path)["c3"]["Status"] == "qualified"
    assert _rows(exporter.csv_path)["c3"]["Fit Score"] == "8.2"

    results = outputs / "application_results.json"
    results.write_text(json.dumps({"successful": [{"job_id": "c3", "status": "applied"}], "failed": []}))
    exporter.export()
    results.write_text(json.dumps({"successful": [], "failed": [{"job_id": "c3", "status": "failed", "error": "x"}]}))
    exporter.export()
    assert _rows(exporter.csv_path)["c3"]["Status"] == "applied"


# ==============================================================================
# AUTO-APPLY GUARDS
# ==============================================================================

def test_agent_does_not_open_a_browser_for_a_login_walled_job(tmp_path):
    from job_agent.automation.agent import AutoApplyAgent

    class _NoBrowser:
        def new_stealth_page(self):
            raise AssertionError("a browser must not be opened")

    class _Store:
        def update_status(self, *_):
            pass

    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    outcome = AutoApplyAgent(session_manager=_NoBrowser(), delta_store=_Store()).apply_to_job(
        profile=None, job=_job(), pdf_resume_path=pdf)
    assert outcome["status"] == "skipped" and outcome["channel"] == "login_required"
    assert not outcome["applied"]


@pytest.mark.parametrize(
    "field,is_search",
    [
        ({"type": "search", "label": "Location"}, True),
        ({"type": "text", "name": "keywords", "label": "Title"}, True),
        ({"type": "text", "id": "jobs-search-box-location-id", "label": "City"}, True),
        ({"type": "text", "name": "location", "label": "Location"}, False),
        ({"type": "email", "name": "email", "label": "Email"}, False),
    ],
)
def test_site_search_boxes_are_never_filled(field, is_search):
    from job_agent.automation.form_filler import FormFiller

    assert FormFiller._is_search_field(field) is is_search


class _Locator:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class _Page:
    def __init__(self, selectors):
        self.selectors = selectors

    def locator(self, selector):
        return _Locator(sum(n for key, n in self.selectors.items() if key in selector))


@pytest.mark.parametrize(
    "selectors,expected",
    [
        ({"type='file'": 1}, True),
        ({"type='email'": 1, "*='name'": 2}, True),
        ({"type='email'": 1}, False),  # a newsletter box
        ({}, False),                   # a listing page with an "Apply" link
    ],
)
def test_submit_requires_an_application_form_on_the_page(selectors, expected):
    from job_agent.automation.navigator import DOMNavigator

    assert DOMNavigator(_Page(selectors)).has_application_form() is expected


@pytest.mark.parametrize(
    "apply_url",
    [
        "https://abb.wd3.myworkdayjobs.com/External_Career_Page/job/Bangalore/SALES_JR00046237",
        "https://wd1.myworkdaysite.com/recruiting/wf/WellsFargoJobs/job/Bengaluru/R-552224",
        "https://www.amazon.jobs/jobs/10544301/assistant-brand-manager",
    ],
)
def test_application_systems_that_require_an_account_go_to_manual_apply(apply_url):
    route = route_application(_job(job_url="https://in.indeed.com/viewjob?jk=1", apply_url=apply_url))
    assert route.channel == "account_required" and not route.automatable and route.url == apply_url


@pytest.mark.parametrize(
    "email,kept",
    [
        ("accommodations@docusign.com", False), ("privacy@acme.com", False), ("press@acme.com", False),
        ("sales@acme.com", False), ("careers-accessibility@acme.com", False), ("hr@acme.com", True),
        ("reportfraud@acme.com", False), ("disabilityrecruitment@acme.com", False), ("askhr@acme.com", True),
    ],
)
def test_mailboxes_for_other_departments_are_not_offered_for_resumes(email, kept):
    from job_agent.contacts.extract import extract_emails

    assert bool(extract_emails(f"Contact {email}")) is kept
