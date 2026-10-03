"""Phase 2 of the production audit: whether the right jobs reach the judge, and are judged fairly.

C4  whole families of on-target jobs were dropped by the title filter
C3  the offline judge could not qualify anything
H2  seniority was ignored unless the posting stated a number
H3  matched skills were stored without being checked against the profile
"""
from __future__ import annotations

import pytest
from test_tailoring import candidate_profile  # noqa: F401

from job_agent.sourcing.relevance import suspicious_role_words, title_matches

TARGETS = ["ai engineer", "ml engineer", "genai", "deep learning", "agentic ai"]


# ------------------------------------------------------------------ C4 ----------

@pytest.mark.parametrize("title", [
    "AI Engineer", "Machine Learning Engineer", "ML Engineer", "Senior AI Engineer", "NLP Engineer",
    "Deep Learning Engineer", "Generative AI Engineer", "GenAI Developer", "Agentic AI Engineer",
    # These were dropped before: none contains the literal tokens "ai", "ml" or "nlp".
    "MLOps Engineer", "LLMOps Engineer", "Computer Vision Engineer", "Applied Scientist",
    "Research Scientist, Machine Learning", "AI Scientist", "RAG Engineer",
    # A department named after a family word is not the role.
    "AI Engineer, Marketing Platform", "Machine Learning Engineer - Sales Analytics",
])
def test_on_target_roles_reach_the_judge(title):
    assert title_matches(title, TARGETS), f"{title!r} was dropped before it could be scored"


@pytest.mark.parametrize("title", [
    "Sales Engineer", "AI Sales Engineer", "Marketing Manager", "Technical Recruiter - AI Teams",
    "Registered Nurse", "Warehouse Associate", "Accountant", "Frontend Developer", "Data Entry Clerk",
    "Mechanical Engineer", "Business Analyst",
])
def test_unrelated_roles_are_still_rejected(title):
    assert not title_matches(title, TARGETS), f"{title!r} should not have been let through"


def test_a_misspelled_target_role_is_caught_with_a_suggestion():
    # `ai enginner` sat in the real config for weeks. It matched nothing, and was masked only
    # because other entries happened to reduce to the same token.
    assert suspicious_role_words("ai enginner") == [("enginner", "engineer")]
    assert suspicious_role_words("machine lerning engineer") == [("lerning", "learning")]
    assert suspicious_role_words("ml engineer") == []
    assert suspicious_role_words("agentic ai") == []
    assert suspicious_role_words("genai") == []


def test_unusual_but_valid_words_are_not_flagged():
    for role in ("Kubernetes Operator Engineer", "Quant Researcher", "Prompt Engineer", "Robotics Engineer"):
        assert suspicious_role_words(role) == [], role


def test_the_configuration_warns_about_a_misspelled_role():
    from job_agent.config.schema import SearchParameters
    from job_agent.sourcing.scraper import OmnichannelScraper

    params = SearchParameters(target_domains=["ai enginner", "ml engineer"], job_boards=["indeed"],
                              find_contacts=False)
    warnings = OmnichannelScraper.configuration_warnings(params)
    assert any("enginner" in w and "engineer" in w for w in warnings), warnings


# ------------------------------------------------------------ C3 / H2 -----------

def _judge(profile, title, description, sim=0.6):
    from job_agent.config.schema import JobPosting
    from job_agent.evaluation.reranker import LLMReranker

    job = JobPosting(id="j1", title=title, company="Acme", description=description,
                     location="Remote", job_url="https://example.com/j1", source="test", is_remote=True)
    return LLMReranker(provider="none")._heuristic_reranker(profile, job, sim)


def _profile(candidate_profile, years):
    return candidate_profile.model_copy(update={"years_of_experience": years})


def test_title_rank_words_imply_a_years_floor():
    from job_agent.evaluation.reranker import LLMReranker

    years = LLMReranker._title_years
    assert years("Senior AI Engineer") == 5.0
    assert years("Staff Machine Learning Engineer") == 8.0
    assert years("Engineering Manager, AI") == 6.0
    assert years("Associate AI Engineer") == 0.0
    assert years("AI Engineer") is None


def test_a_senior_title_is_not_a_good_fit_for_a_junior_candidate(candidate_profile):
    # No years figure anywhere in the posting: only the title says "Senior".
    junior = _profile(candidate_profile, 1.5)
    senior_role = _judge(junior, "Senior AI Engineer", "Build LLM systems in Python.")
    plain_role = _judge(junior, "AI Engineer", "Build LLM systems in Python.")
    assert senior_role.fit_score < plain_role.fit_score
    assert senior_role.fit_score < 7.0


def test_a_title_that_outranks_the_body_still_counts(candidate_profile):
    junior = _profile(candidate_profile, 1.5)
    verdict = _judge(junior, "Principal AI Engineer", "Requires 2+ years of Python.")
    assert "8 required" in verdict.reasoning
    assert verdict.fit_score <= 6.0


def test_a_well_matched_role_qualifies_without_an_api_key(candidate_profile):
    skills = candidate_profile.skills.all_skills()[:6]
    description = "We build products with " + ", ".join(skills) + "."
    verdict = _judge(candidate_profile, "AI Engineer", description, sim=0.62)
    assert verdict.fit_score >= 7.0, verdict.reasoning


def test_an_unrelated_role_does_not_qualify(candidate_profile):
    verdict = _judge(candidate_profile, "Warehouse Associate", "Pick and pack orders. Forklift licence.", sim=0.1)
    assert verdict.fit_score < 5.0


def test_skill_overlap_alone_cannot_reach_a_perfect_score(candidate_profile):
    skills = candidate_profile.skills.all_skills()
    verdict = _judge(candidate_profile, "AI Engineer", " ".join(skills), sim=0.3)
    assert verdict.technical_score < 10.0


# ---------------------------------------------------------------- H3 ------------

def test_matching_skills_must_be_in_both_profile_and_posting(candidate_profile, monkeypatch):
    from job_agent.config.schema import JobPosting
    from job_agent.evaluation.reranker import LLMReranker

    owned = candidate_profile.skills.all_skills()
    in_posting, not_in_posting = owned[0], owned[1]
    job = JobPosting(id="j9", title="AI Engineer", company="Acme", location="Remote",
                     description=f"You will use {in_posting} daily and Fortran.", is_remote=True,
                     job_url="https://example.com/j9", source="test")
    reranker = LLMReranker(provider="groq")
    monkeypatch.setattr(reranker, "_call_groq", lambda *a, **k: {
        "fit_score": 8.0, "technical_score": 8.0, "seniority_score": 8.0, "reasoning": "x",
        # one real, one the posting never mentions, one the candidate never listed
        "matching_skills": [in_posting, not_in_posting, "Fortran"],
        "missing_skills": ["Fortran", owned[2]],
    })
    score = reranker.evaluate_job(candidate_profile, job, 0.6)
    assert score.matching_skills == [in_posting]
    assert score.missing_skills == ["Fortran"]


# ------------------------------------------------- thresholds / location / currency

def test_tier1_cutoff_follows_the_embedding_backend(monkeypatch):
    from job_agent.evaluation.embedder import SemanticEmbedder

    embedder = SemanticEmbedder()
    monkeypatch.setattr(embedder, "_get_encoder", lambda: None)
    embedder._use_fallback = False
    assert embedder.resolve_threshold(None) == SemanticEmbedder.TRANSFORMER_THRESHOLD
    embedder._use_fallback = True
    assert embedder.resolve_threshold(None) == SemanticEmbedder.TFIDF_THRESHOLD
    assert embedder.resolve_threshold(0.05) == 0.05   # an operator's value always wins
    assert SemanticEmbedder.TRANSFORMER_THRESHOLD > SemanticEmbedder.TFIDF_THRESHOLD


@pytest.mark.parametrize("location", ["Nagpur", "Mysuru, Karnataka", "Navi Mumbai", "Bhubaneswar, Odisha", "Vizag"])
def test_smaller_indian_cities_resolve_to_india(location):
    from job_agent.config.schema import location_country

    assert location_country(location) == "india"


def test_a_blank_board_currency_is_unknown_not_dollars(tmp_path):
    import pandas as pd

    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.config.schema import SearchParameters
    from job_agent.sourcing.scraper import OmnichannelScraper

    scraper = OmnichannelScraper(search_params=SearchParameters(
        target_domains=["AI Engineer"], is_remote=True, find_contacts=False, min_salary=100000,
        salary_currency="USD"), delta_store=DeltaStore(tmp_path / "d.db"))
    frame = pd.DataFrame([{"title": "AI Engineer", "company": "Acme", "job_url": "https://acme.example/1",
                           "description": "x" * 80, "location": "Remote", "is_remote": True,
                           "min_amount": 5000, "max_amount": 9000, "currency": None, "site": "indeed"}])
    postings = scraper._normalize_jobspy_df(frame)
    assert postings, "the row should have normalised into a posting"
    assert postings[0].salary_currency is None
    # An unknown currency is never compared with the floor, so the posting survives.
    assert scraper._passes_filters(postings[0])


# --------------------------------------------------------- Phase 3 / H6 -----------

def _bare_scraper(tmp_path, **params):
    from job_agent.config.schema import SearchParameters
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.sourcing.scraper import OmnichannelScraper

    base = dict(target_domains=["AI Engineer", "ML Engineer"], locations=["Remote", "Pune"], is_remote=True,
                find_contacts=False, job_boards=["indeed", "linkedin", "bayt"])
    base.update(params)
    return OmnichannelScraper(search_params=SearchParameters(**base), delta_store=DeltaStore(tmp_path / "d.db"))


def test_boards_are_swept_concurrently_and_each_in_order(tmp_path, monkeypatch):
    import sys
    import threading
    import types

    scraper = _bare_scraper(tmp_path)
    calls, started = [], set()
    # Every board must be inside its first request at the same moment, or the barrier times out.
    rendezvous = threading.Barrier(3, timeout=10)

    def fake_single(self, jobspy, *, board, domain, location, results_wanted):
        calls.append((board, domain, location))
        if board not in started:
            started.add(board)
            rendezvous.wait()
        return []

    monkeypatch.setitem(sys.modules, "jobspy", types.ModuleType("jobspy"))
    monkeypatch.setattr(type(scraper), "_scrape_single_board", fake_single)
    scraper.scrape_job_boards()

    # Within a board every (role, location) pair is still queried, in order.
    assert [c[1:] for c in calls if c[0] == "indeed"] == [
        ("AI Engineer", "Remote"), ("AI Engineer", "Pune"), ("ML Engineer", "Remote"), ("ML Engineer", "Pune")]
    # Bayt ignores location: once per role, not once per city.
    assert [c[1:] for c in calls if c[0] == "bayt"] == [("AI Engineer", "Remote"), ("ML Engineer", "Remote")]


def test_a_stop_request_reaches_the_board_workers(tmp_path, monkeypatch):
    import sys
    import threading
    import types

    from job_agent.runtime import RunCancelled, cancellation

    scraper = _bare_scraper(tmp_path)
    monkeypatch.setitem(sys.modules, "jobspy", types.ModuleType("jobspy"))
    monkeypatch.setattr(type(scraper), "_scrape_single_board", lambda self, *a, **k: [])
    stop = threading.Event()
    stop.set()
    with cancellation(stop):
        with pytest.raises(RunCancelled):
            scraper.scrape_job_boards()


def test_the_country_patch_is_shared_across_threads_and_fully_restored():
    pytest.importorskip("jobspy")
    import threading

    from jobspy.model import Country

    from job_agent.sourcing.scraper import tolerate_unknown_countries

    before = Country.__dict__["from_string"]
    barrier, inside = threading.Barrier(4), []

    def worker():
        with tolerate_unknown_countries():
            barrier.wait(timeout=10)
            inside.append(Country.from_string("Atlantis") == Country.WORLDWIDE)
            barrier.wait(timeout=10)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    assert inside == [True] * 4
    assert Country.__dict__["from_string"] is before, "JobSpy must be restored once the last thread leaves"


# --------------------------------------------- offline judge: work-authorization branch

def test_authorization_is_never_assumed_false_but_an_explicit_no_caps_the_score(candidate_profile, monkeypatch):
    from job_agent.config.schema import JobPosting, WorkAuthorization
    from job_agent.evaluation.reranker import LLMReranker

    auth = candidate_profile.work_authorization.model_copy(update={
        "current_country": "India", "authorized_countries": ["India"], "requires_sponsorship": True})
    profile = candidate_profile.model_copy(update={"work_authorization": auth})
    skills = ", ".join(profile.skills.all_skills()[:6])

    def verdict(location, remote):
        job = JobPosting(id="v1", title="AI Engineer", company="Acme", location=location, is_remote=remote,
                         description=f"Build with {skills}.", job_url="https://example.com/v1", source="test")
        return LLMReranker(provider="none")._heuristic_reranker(profile, job, 0.65)

    # By design a location the profile does not list is UNKNOWN, not "not authorized" (a second
    # citizenship may simply be unstated), so it is not penalised and is left for a human.
    assert WorkAuthorization.is_authorized_in(auth, "London, United Kingdom") is None
    assert verdict("London, United Kingdom", False).fit_score > 4.0

    # When authorization is explicitly known to be absent, an on-site role is capped; a remote one is not.
    monkeypatch.setattr(WorkAuthorization, "is_authorized_in", lambda self, location: False)
    capped = verdict("London, United Kingdom", False)
    assert capped.fit_score <= 4.0 and "work authorization" in capped.reasoning
    assert verdict("London, United Kingdom", True).fit_score > 4.0


# ------------------------------------------------------ Phase 5: skipped-job records

def test_every_dropped_listing_is_recorded_with_its_reason(tmp_path):
    from job_agent.config.schema import JobPosting

    scraper = _bare_scraper(tmp_path, onsite_countries=["india"], is_remote=True)

    def job(title, location, remote):
        return JobPosting(id=title.replace(" ", "-")[:40], title=title, company="Acme", location=location, is_remote=remote,
                          job_url=f"https://acme.example/{title}", source="indeed", description="x" * 80)

    assert scraper._passes_filters(job("AI Engineer", "Remote", True))
    assert not scraper._passes_filters(job("Sales Manager", "Remote", True))
    assert not scraper._passes_filters(job("AI Engineer", "London, United Kingdom", False))

    assert [(r["reason"], r["title"]) for r in scraper.skipped] == [
        ("off_target", "Sales Manager"), ("outside_onsite_countries", "AI Engineer")]
    assert scraper.skipped[1]["location"] == "London, United Kingdom"
    assert scraper.filter_stats["off_target"] == 1 and scraper.filter_stats["outside_onsite_countries"] == 1


def test_the_record_list_is_bounded(tmp_path, monkeypatch):
    import job_agent.sourcing.scraper as scraper_module

    monkeypatch.setattr(scraper_module, "MAX_SKIPPED_RECORDS", 2)
    scraper = _bare_scraper(tmp_path)
    for n in range(5):
        scraper._reject("off_target", None, title=f"t{n}", company="c", url="u")
    assert len(scraper.skipped) == 2 and scraper.filter_stats["off_target"] == 5   # counts stay exact


def test_a_new_candidate_does_not_inherit_the_previous_ones_skips():
    from job_agent.intake.switch import CANDIDATE_FILES
    from job_agent.sourcing.delta_store import DeltaStore
    from job_agent.tracking.skips import SKIPS_FILE

    assert SKIPS_FILE in CANDIDATE_FILES
    assert "skipped" in DeltaStore._REOPENABLE


# ------------------------------------------------------ completing the remaining items

def test_a_wide_gap_between_the_search_level_and_the_resume_is_flagged():
    from job_agent.config.schema import SearchParameters
    from job_agent.sourcing.scraper import OmnichannelScraper

    params = SearchParameters(target_domains=["AI Engineer"], desired_experience_years=4.0)
    warnings = OmnichannelScraper.configuration_warnings(params, profile_years=1.7)
    assert any("4 years" in w and "1.7" in w and "desired_experience_years" in w for w in warnings), warnings
    assert not any("desired_experience_years" in w
                   for w in OmnichannelScraper.configuration_warnings(params, profile_years=3.5))
    assert not any("desired_experience_years" in w for w in OmnichannelScraper.configuration_warnings(params))


def test_duplicates_and_already_seen_jobs_are_recorded_too(tmp_path, monkeypatch):
    """Every way a listing can disappear from a sweep now leaves a record."""
    import sys
    import types

    from job_agent.config.schema import JobPosting
    from job_agent.sourcing.delta_store import DeltaStore

    def posting(job_id, source, url):
        return JobPosting(id=job_id, title="AI Engineer", company="Acme", location="Remote", is_remote=True,
                          job_url=url, source=source, description="Build LLM systems. " * 10,
                          date_posted=None)

    scraper = _bare_scraper(tmp_path, job_boards=["indeed"], find_contacts=False)
    monkeypatch.setitem(sys.modules, "jobspy", types.ModuleType("jobspy"))
    seen = posting("seen-1", "indeed", "https://acme.example/old")
    other_company = JobPosting(id="fresh-1", title="ML Engineer", company="Globex", location="Remote",
                               is_remote=True, job_url="https://globex.example/1", source="indeed",
                               description="Train models. " * 10)
    DeltaStore(tmp_path / "d.db").mark_many_seen([seen])
    batch = [posting("dup-a", "indeed", "https://acme.example/a"), posting("dup-b", "linkedin", "https://acme.example/b"),
             other_company]
    monkeypatch.setattr(type(scraper), "scrape_job_boards", lambda self: batch)
    monkeypatch.setattr(type(scraper), "_find_contacts", lambda self, jobs: jobs)
    scraper.run_sourcing_pipeline(include_ats_direct=False, output_file=tmp_path / "scraped.json")

    reasons = [(r["reason"], r["title"]) for r in scraper.skipped]
    assert ("duplicate_in_sweep", "AI Engineer") in reasons
    assert ("already_seen", "AI Engineer") in reasons
    assert all(r.get("detail") for r in scraper.skipped if r["reason"] in ("duplicate_in_sweep", "already_seen"))


def test_the_same_role_in_two_cities_is_one_role_by_design():
    from job_agent.config.schema import job_fingerprint

    # The outreach ledger keys on this: a second city must not mean a second email to the employer.
    assert job_fingerprint("Acme Inc", "AI Engineer") == job_fingerprint("ACME", "AI Engineer (Remote)")
