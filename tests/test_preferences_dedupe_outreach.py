"""Candidate preferences, strict freshness, cross-board de-duplication, and outreach that never repeats."""

from __future__ import annotations

import csv
import email
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from job_agent.config.normalize import utc_now_iso
from job_agent.config.schema import (
    CandidateProfile,
    ContactInfo,
    JobPosting,
    SearchParameters,
    SkillSet,
    WorkAuthorization,
    job_fingerprint,
    location_country,
)
from job_agent.config.settings import settings
from job_agent.sourcing.delta_store import DeltaStore


def _profile(**auth) -> CandidateProfile:
    profile = CandidateProfile(
        contact=ContactInfo(full_name="Raj Aryan", email="raj@example.org", phone="+91-9876543210", location="Gurugram"),
        summary="Product manager with AI and analytics experience.",
        work_authorization=WorkAuthorization(**auth),
        skills=SkillSet(languages=["Python", "SQL"]),
        years_of_experience=4.0,
    )
    return profile.seal_profile()


INDIA = dict(current_country="India", authorized_countries=["India"], requires_sponsorship=True, remote_worldwide=True)


def _job(**overrides) -> JobPosting:
    data = dict(id="j1", title="Product Manager", company="Acme", location="Remote", is_remote=True,
                job_url="https://www.linkedin.com/jobs/view/1", source="linkedin")
    data.update(overrides)
    return JobPosting(**data)


# ==============================================================================
# LOCATIONS AND ELIGIBILITY
# ==============================================================================

@pytest.mark.parametrize(
    "location,country",
    [
        ("Bengaluru, Karnataka, India", "india"), ("Gurugram, HR, IN", "india"), ("HR, IN", "india"),
        ("Bangalore", "india"), ("Pune, Maharashtra", "india"), ("Remote", None),
        ("San Francisco, CA", "usa"), ("Indianapolis, IN", None), ("Austin, TX, US", "usa"),
        ("London, England, United Kingdom", "uk"), ("Toronto, ON, CA", "canada"),
    ],
)
def test_location_country_uses_only_explicit_evidence(location, country):
    assert location_country(location) == country


@pytest.mark.parametrize(
    "location,remote,expected",
    [
        ("Bengaluru, Karnataka, India", False, False),  # home country
        ("Remote", True, False),                        # remote from India
        ("London, England, United Kingdom", False, True),  # relocation
        ("New York, NY, US", True, False),              # remote for a US employer
        ("Somewhere", False, None),                     # cannot tell
    ],
)
def test_sponsorship_is_needed_only_to_work_abroad(location, remote, expected):
    assert WorkAuthorization(**INDIA).needs_sponsorship_for(location, is_remote=remote) is expected


def test_screening_answers_follow_the_job_location():
    from job_agent.automation.form_filler import FormFiller

    profile = _profile(**INDIA)
    ask = lambda job, q: FormFiller(profile, job).answer_screening_question(q)

    india = _job(location="Mumbai, Maharashtra, India", is_remote=False)
    london = _job(location="London, England, United Kingdom", is_remote=False)
    assert ask(india, "Will you require visa sponsorship?") == "No"
    assert ask(india, "Are you legally authorized to work?") == "Yes"
    assert ask(london, "Will you require visa sponsorship?") == "Yes"
    # A country not on the list stays unknown for a human, never a guessed "No".
    assert ask(london, "Are you legally authorized to work?") is None
    assert ask(_job(), "Will you require sponsorship to work in the United Kingdom?") == "Yes"


def test_salary_expectation_answers():
    from job_agent.automation.form_filler import FormFiller

    data = _profile(**INDIA).model_dump()
    data.update(desired_salary=1_000_000, desired_salary_max=1_400_000, salary_currency="INR")
    profile = CandidateProfile.model_validate(data)
    filler = FormFiller(profile, _job())

    assert profile.salary_expectation_text() == "INR 10,00,000 - 14,00,000 per year (10-14 LPA)"
    assert filler.salary_answer("expected ctc (in lpa)") == "14"
    assert filler.salary_answer("expected salary", numeric=True) == "1400000"
    assert filler.answer_screening_question("What is your current CTC?") is None


def test_salary_range_must_not_be_inverted():
    data = _profile(**INDIA).model_dump()
    data.update(desired_salary=1_400_000, desired_salary_max=1_000_000, salary_currency="INR")
    with pytest.raises(Exception):
        CandidateProfile.model_validate(data)


def test_profiles_sealed_before_these_fields_existed_still_verify():
    profile = _profile(current_country="India")
    payload = json.loads(profile.model_dump_json())
    for key in ("desired_salary_max", "salary_currency"):
        payload.pop(key)
    payload["work_authorization"].pop("remote_worldwide")
    assert CandidateProfile.model_validate(payload).verify_integrity()


# ==============================================================================
# PREFERENCES
# ==============================================================================

def test_preferences_apply_to_the_profile_and_survive_a_new_resume(tmp_path, monkeypatch):
    from job_agent.intake.preferences import reapply_saved_preferences, save_preferences

    profile_path = tmp_path / "profiles" / "profile.json"
    profile_path.parent.mkdir()
    profile_path.write_text(_profile(current_country="Unspecified").model_dump_json(), encoding="utf-8")
    monkeypatch.setattr(settings, "profile_path", profile_path)

    updated = save_preferences(dict(current_country="india", authorized_countries="India", requires_sponsorship=True,
                                    remote_worldwide=True, desired_salary=1_000_000, desired_salary_max=1_400_000,
                                    salary_currency="inr"))
    assert updated.verify_integrity()
    assert updated.work_authorization.current_country == "India"
    assert updated.salary_currency == "INR"

    # Intake on a new resume produces a profile without them; they carry over.
    fresh = _profile(current_country="Unspecified")
    carried = reapply_saved_preferences(fresh, profile_path)
    assert carried.work_authorization.requires_sponsorship is True
    assert carried.desired_salary_max == 1_400_000
    assert carried.verify_integrity()


def test_preferences_refuse_a_profile_whose_facts_were_edited(tmp_path, monkeypatch):
    from job_agent.intake.preferences import save_preferences

    profile_path = tmp_path / "profile.json"
    data = json.loads(_profile(current_country="India").model_dump_json())
    data["years_of_experience"] = 12.0
    profile_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(settings, "profile_path", profile_path)
    with pytest.raises(ValueError, match="seal"):
        save_preferences({"current_country": "India"})


def test_a_salary_needs_a_currency():
    from job_agent.intake.preferences import CandidatePreferences

    with pytest.raises(Exception, match="currency"):
        CandidatePreferences(desired_salary=1_000_000)


# ==============================================================================
# SOURCING FILTERS
# ==============================================================================

def _scraper(tmp_path, **params):
    from job_agent.sourcing.scraper import OmnichannelScraper

    base = dict(target_domains=["Product Manager"], is_remote=True, hours_old=48, find_contacts=False)
    base.update(params)
    return OmnichannelScraper(search_params=SearchParameters(**base), delta_store=DeltaStore(tmp_path / "d.db"))


def test_remote_anywhere_plus_onsite_at_home(tmp_path):
    scraper = _scraper(tmp_path, onsite_countries=["india"])
    assert scraper._passes_filters(_job(location="Bengaluru, Karnataka, India", is_remote=False))
    assert scraper._passes_filters(_job(location="Remote", is_remote=True))
    assert not scraper._passes_filters(_job(location="London, England, United Kingdom", is_remote=False))
    assert not scraper._passes_filters(_job(location="Somewhere", is_remote=False))


def test_time_window_is_strict(tmp_path):
    scraper = _scraper(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    assert not scraper._passes_filters(_job(date_posted=old))
    assert scraper._passes_filters(_job(date_posted=utc_now_iso()))
    # Boards apply the window themselves; company feeds list every open role.
    assert scraper._passes_filters(_job(date_posted=None))
    assert not scraper._passes_filters(_job(date_posted=None, source="greenhouse",
                                            job_url="https://job-boards.greenhouse.io/acme/jobs/1"))
    assert scraper.filter_stats["undated"] == 1 and scraper.filter_stats["too_old"] == 1


# ==============================================================================
# DE-DUPLICATION
# ==============================================================================

def test_fingerprint_ignores_board_and_company_suffixes():
    assert job_fingerprint("Cimpress India Pvt. Ltd.", "Product Manager - Remote") == \
        job_fingerprint("Cimpress", "Product Manager")
    assert job_fingerprint("Acme", "Product Manager") != job_fingerprint("Acme", "Senior Product Manager")


def test_same_role_on_two_boards_is_kept_once_with_both_boards_contacts(tmp_path):
    scraper = _scraper(tmp_path)
    linkedin = _job(id="li", contacts=[{"email": "talent@acme.com", "kind": "hiring", "source": "job_post"}])
    indeed = _job(id="in", job_url="https://in.indeed.com/viewjob?jk=9", source="indeed",
                  company="Acme Pvt Ltd", apply_url="https://careers.acme.com/jobs/9",
                  contacts=[{"email": "info@acme.com", "kind": "general", "source": "company_site"}])
    kept, duplicates = scraper._deduplicate([linkedin, indeed])

    assert duplicates == 1 and len(kept) == 1
    # The copy with a usable application form wins.
    assert kept[0].id == "in"
    assert {c.email for c in kept[0].contacts} == {"talent@acme.com", "info@acme.com"}


def test_a_role_seen_in_an_earlier_run_is_not_new_on_another_board(tmp_path):
    store = DeltaStore(tmp_path / "d.db")
    store.mark_many_seen([_job(id="li")])
    repost = _job(id="in", job_url="https://in.indeed.com/viewjob?jk=9", source="indeed")
    other = _job(id="x", title="Data Analyst", job_url="https://in.indeed.com/viewjob?jk=10")
    assert [job.id for job in store.filter_unseen([repost, other])] == ["x"]


def test_older_databases_are_migrated_with_fingerprints(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE seen_jobs (job_id TEXT PRIMARY KEY, job_url TEXT, company TEXT, title TEXT,
                        source TEXT, first_seen_at TEXT, status TEXT DEFAULT 'scraped')""")
        conn.execute("INSERT INTO seen_jobs VALUES ('old', 'u', 'Acme', 'Product Manager', 'linkedin', 't', 'scraped')")
    store = DeltaStore(path)
    assert store.filter_unseen([_job(id="new", job_url="https://in.indeed.com/viewjob?jk=1")]) == []


# ==============================================================================
# OUTREACH
# ==============================================================================

def test_outreach_is_drafted_once_per_address_and_role(tmp_path):
    from job_agent.tracking import outreach

    store = DeltaStore(tmp_path / "d.db")
    job = _job(contacts=[{"email": "Careers@Acme.com", "kind": "hiring", "source": "job_post"}])

    first = outreach.plan_outreach(store, job)
    assert first.status == outreach.READY and first.recipient == "careers@acme.com"
    store.record_outreach(first.recipient, job, "Subject", "Body")

    again = outreach.plan_outreach(store, job)
    assert again.status == outreach.DRAFTED_BEFORE and "do not send again" in again.note

    # The same role found later under a different listing.
    repost = _job(id="other", company="Acme Pvt Ltd", job_url="https://in.indeed.com/viewjob?jk=2",
                  contacts=[{"email": "careers@acme.com", "kind": "hiring", "source": "company_site"}])
    assert outreach.plan_outreach(store, repost).status == outreach.DRAFTED_BEFORE

    # A different role at the same inbox within the cooldown is held.
    second_role = _job(id="r2", title="Data Analyst",
                       contacts=[{"email": "careers@acme.com", "kind": "hiring", "source": "job_post"}])
    assert outreach.plan_outreach(store, second_role).status == outreach.HOLD
    later = datetime.now(timezone.utc) + timedelta(days=outreach.COOLDOWN_DAYS + 1)
    assert outreach.plan_outreach(store, second_role, now=later).status == outreach.READY


def test_jobs_without_an_email_still_get_a_draft_marked_for_manual_use(tmp_path):
    from job_agent.tracking import outreach

    plan = outreach.plan_outreach(DeltaStore(tmp_path / "d.db"), _job())
    assert plan.status == outreach.NO_EMAIL and plan.recipient is None


def test_email_draft_file_opens_as_unsent_with_resume_attached(tmp_path):
    from job_agent.tracking import outreach

    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4 test")
    path = outreach.write_eml(tmp_path / "d.eml", _profile(**INDIA), "careers@acme.com",
                              "Product Manager - Raj Aryan", "Hello", pdf)
    message = email.message_from_bytes(path.read_bytes())
    assert message["To"] == "careers@acme.com" and message["X-Unsent"] == "1"
    attachments = [part.get_filename() for part in message.walk() if part.get_filename()]
    assert attachments == ["Raj_Aryan_Resume.pdf"]


def test_split_subject():
    from job_agent.tracking.outreach import split_subject

    assert split_subject("Subject: Hi there\n\nBody line") == ("Hi there", "Body line")
    assert split_subject("No subject") == ("", "No subject")


def test_tracking_twice_drafts_once_and_the_csv_carries_the_email(tmp_path, monkeypatch):
    from job_agent.tracking.export import JobsCsvExporter
    from job_agent.tracking.pipeline import FallbackTrackingPipeline
    from job_agent.tracking.tracker import MasterTracker

    outputs = settings.outputs_dir
    outputs.mkdir(parents=True, exist_ok=True)
    profile = _profile(**INDIA)
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(profile.model_dump_json(), encoding="utf-8")

    job = _job(contacts=[{"email": "hr@acme.com", "kind": "hiring", "source": "job_post"}])
    qualified = outputs / "qualified_jobs.json"
    qualified.write_text(json.dumps([{"job": job.model_dump(), "evaluation": {
        "embedding_similarity": 0.5, "fit_score": 8.0, "technical_score": 8.0, "seniority_score": 8.0,
        "threshold_used": 7.0, "passed_threshold": True, "reasoning": "Strong overlap with the role.",
        "matching_skills": [], "missing_skills": [], "scored_by": "test"}}]), encoding="utf-8")

    calls = []

    class _Generator:
        def generate_email(self, profile, job, fit_score=8.0):
            calls.append(job.id)
            return "Subject: Product Manager - Raj Aryan\n\nHi Acme Hiring Team,\n\nBody."

    store = DeltaStore(outputs / "delta.db")
    make = lambda: FallbackTrackingPipeline(email_generator=_Generator(), delta_store=store,
                                            tracker=MasterTracker(tmp_path / "t.xlsx"))
    make().process_fallbacks(qualified_jobs_path=qualified, profile_path=profile_path, force_track_all=True)
    make().process_fallbacks(qualified_jobs_path=qualified, profile_path=profile_path, force_track_all=True)

    assert calls == ["j1"]
    assert (outputs / "outreach" / "j1.eml").is_file()

    exporter = JobsCsvExporter(outputs_dir=outputs)
    exporter.export()
    with exporter.csv_path.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["HR / Careers Email"] == "hr@acme.com"
    assert row["Outreach To"] == "hr@acme.com"
    assert row["Cold Email Subject"] == "Product Manager - Raj Aryan"
    assert "Hi Acme Hiring Team" in row["Cold Email Body"]
    assert "do not send again" in row["Outreach Status"]
    assert row["Email Draft File"] == "j1.eml"


# ==============================================================================
# RELEVANCE AND BACKLOG (from the first live end-to-end run)
# ==============================================================================

ROLES = ["Associate Product Manager", "AI Product Manager", "AI ML Engineer"]


@pytest.mark.parametrize(
    "title,relevant",
    [
        ("Product Manager", True), ("Senior Product Manager", True), ("Technical Product Manager - Gen AI", True),
        ("Machine Learning Engineer", True), ("GenAI Engineer", True), ("AI Full stack Developer", True),
        ("Product Lead - Fraud & Claims", True), ("Applied AI/ML Engineer", True),
        ("Wholesale Credit Risk Associate - Quantitative Research", False), ("Business Analyst", False),
        ("Merchandiser Ecommerce", False), ("Program Manager I, FBA", False), ("Data Scientist II-3", False),
    ],
)
def test_board_titles_must_name_a_target_role(title, relevant):
    from job_agent.sourcing.relevance import title_matches

    assert title_matches(title, ROLES) is relevant


def test_off_target_board_jobs_are_dropped_but_company_feeds_are_not_refiltered(tmp_path):
    scraper = _scraper(tmp_path, target_domains=ROLES)
    assert not scraper._passes_filters(_job(title="Business Analyst"))
    assert scraper.filter_stats["off_target"] == 1
    assert scraper._passes_filters(_job(title="AI Engineer"))


def test_jobs_evaluation_never_reached_carry_over_to_the_next_sweep(tmp_path):
    scraper = _scraper(tmp_path, target_domains=ROLES)
    previous = tmp_path / "scraped_jobs.json"
    waiting = _job(id="waiting", date_posted=utc_now_iso())
    evaluated = _job(id="done", title="AI Engineer", job_url="https://www.linkedin.com/jobs/view/2",
                     date_posted=utc_now_iso())
    stale = _job(id="stale", title="Senior Product Manager", job_url="https://www.linkedin.com/jobs/view/3",
                 date_posted=(datetime.now(timezone.utc) - timedelta(hours=60)).isoformat())
    previous.write_text(json.dumps([j.model_dump() for j in (waiting, evaluated, stale)]), encoding="utf-8")
    scraper.delta_store.mark_many_seen([waiting, evaluated, stale])
    scraper.delta_store.update_status("done", "evaluated")

    backlog = scraper._unevaluated_backlog(previous, exclude=set())
    assert [job.id for job in backlog] == ["waiting"]
    assert not scraper.filter_stats  # the funnel reports only this sweep


@pytest.mark.parametrize(
    "email,kind",
    [("askhr@sc.com", "hiring"), ("talentacquisitionindia@revantage.com", "hiring"),
     ("hrithik@acme.com", "person"), ("hrindia@acme.com", "hiring")],
)
def test_run_together_hiring_mailboxes_are_recognised(email, kind):
    from job_agent.contacts.extract import classify_email

    assert classify_email(email) == kind


def test_company_websites_contribute_only_role_mailboxes(tmp_path):
    from job_agent.contacts.finder import CompanySiteCrawler

    class _Response:
        status_code = 200
        headers = {"Content-Type": "text/html"}
        encoding = "utf-8"

        def __init__(self, text):
            body = text.encode()
            self.raw = type("Raw", (), {"read": lambda self, n, decode_content=True: body})()

        def close(self):
            pass

    class _Session:
        headers = {}

        def get(self, url, **_):
            if url.endswith("robots.txt"):
                return _Response("")
            return _Response("Press: anita.rao@acme.com. Jobs: careers@acme.com. Alumni: alumni.network@acme.com")

    found = {c["email"] for c in CompanySiteCrawler(session=_Session(), max_pages=1).find("https://acme.com")}
    assert found == {"careers@acme.com"}


# ==============================================================================
# PER-JOB TAILORING
# ==============================================================================

def test_each_job_gets_its_own_skill_and_project_order_without_new_facts():
    from job_agent.config.schema import Project
    from job_agent.tailoring.rewriter import ResumeTailorer

    data = _profile(**INDIA).model_dump()
    data["skills"] = {"languages": ["SQL", "Python"], "domain_knowledge": ["A/B testing", "RAG", "Roadmapping"]}
    data["projects"] = [
        {"title": "Growth Dashboard", "description": "Funnel analytics and A/B testing for onboarding", "technologies": ["SQL"]},
        {"title": "RAG Assistant", "description": "Retrieval augmented generation over PDFs with LLM agents", "technologies": ["Python", "FAISS"]},
    ]
    profile = CandidateProfile.model_validate(data).seal_profile()
    tailorer = ResumeTailorer(provider="none")

    pm = _job(title="Associate Product Manager", description="Own roadmapping, run A/B testing, write SQL.")
    ai = _job(id="j2", title="AI Engineer", description="Build RAG pipelines and LLM agents in Python.")
    pm_resume = tailorer.generate_tailored_profile_data(profile, pm)
    ai_resume = tailorer.generate_tailored_profile_data(profile, ai)

    assert pm_resume["skills"]["domain_knowledge"][:2] == ["A/B testing", "Roadmapping"]
    assert ai_resume["skills"]["domain_knowledge"][0] == "RAG"
    assert ai_resume["skills"]["languages"] == ["Python", "SQL"]
    assert pm_resume["projects"][0]["title"] == "Growth Dashboard"
    assert ai_resume["projects"][0]["title"] == "RAG Assistant"
    # Reordered, never changed: the same skills and projects in both.
    for resume in (pm_resume, ai_resume):
        assert sorted(resume["skills"]["domain_knowledge"]) == sorted(data["skills"]["domain_knowledge"])
        assert sorted(p["title"] for p in resume["projects"]) == ["Growth Dashboard", "RAG Assistant"]
        assert resume["summary"] == profile.summary


# ==============================================================================
# OPENING TAILORED RESUMES
# ==============================================================================

def test_tracker_resume_cells_link_to_the_pdf_and_flag_missing_files(tmp_path):
    from job_agent.tracking.tracker import JOB_ID_COLUMN, RESUME_COLUMN, MasterTracker

    resumes = settings.outputs_dir / "tailored_resumes"
    resumes.mkdir(parents=True, exist_ok=True)
    (resumes / "resume_present.pdf").write_bytes(b"%PDF-1.4")
    (resumes / "resume_later.pdf").write_bytes(b"%PDF-1.4")

    tracker = MasterTracker(tmp_path / "tracker.xlsx")
    rows = {}
    for job_id, pdf in (("present", "resume_present.pdf"), ("gone", "resume_gone.pdf"), ("later", None)):
        rows[job_id] = tracker.log_application(
            job=_job(id=job_id, job_url=f"https://www.linkedin.com/jobs/view/{job_id}"),
            match_score=8.0, status="QUEUED", cold_email="", pdf_path=str(resumes / pdf) if pdf else None,
            autosave=False)
    tracker.save()
    cell = lambda job_id: tracker.ws.cell(row=rows[job_id], column=RESUME_COLUMN)

    assert cell("present").value == "resume_present.pdf"
    assert cell("present").hyperlink.target == (resumes / "resume_present.pdf").resolve().as_uri()
    assert "no longer exists" in cell("gone").value and cell("gone").hyperlink is None
    # Logged before its resume existed ("N/A"), then linked once the PDF is there.
    assert cell("later").value == "resume_later.pdf" and cell("later").hyperlink is not None


def test_csv_carries_the_full_path_of_each_existing_resume(tmp_path):
    from job_agent.tracking.export import JobsCsvExporter

    out = tmp_path / "out"
    (out / "tailored_resumes").mkdir(parents=True)
    (out / "tailored_resumes" / "resume_j1.pdf").write_bytes(b"%PDF-1.4")
    job = _job()
    (out / "scraped_jobs.json").write_text(json.dumps([job.model_dump()]), encoding="utf-8")
    (out / "tailored_resumes" / "manifest.json").write_text(json.dumps(
        [{"job_id": "j1", "pdf_path": str(out / "tailored_resumes" / "resume_j1.pdf")}]), encoding="utf-8")

    exporter = JobsCsvExporter(outputs_dir=out)
    exporter.export()
    with exporter.csv_path.open(encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    assert row["Tailored Resume"] == "resume_j1.pdf"
    assert Path(row["Tailored Resume Path"]).is_file()
