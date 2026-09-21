"""Contact discovery: published emails only, correctly classified, never guessed."""

from __future__ import annotations

import pytest

from job_agent.contacts.extract import classify_email, extract_emails, registrable_domain


def emails(text):
    return [email for email, _ in extract_emails(text)]


# ==============================================================================
# EXTRACTION
# ==============================================================================

def test_plain_addresses_in_a_job_post_are_found():
    post = "Interested candidates can share their resume at careers@osfin.ai or call us."
    assert emails(post) == ["careers@osfin.ai"]


def test_trailing_punctuation_is_not_part_of_the_address():
    assert emails("Mail your CV to hr@acme.co.in.") == ["hr@acme.co.in"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Send CV to hr [at] acme [dot] com", "hr@acme.com"),
        ("talent(at)acme(dot)co(dot)in", "talent@acme.co.in"),
        ("jobs {at} startup {dot} io", "jobs@startup.io"),
    ],
)
def test_obfuscated_addresses_are_decoded(text, expected):
    assert emails(text) == [expected]


def test_prose_containing_at_and_dot_is_not_decoded():
    """Unbracketed "at" and "dot" are ordinary English, not an address."""
    assert emails("Meet the team at our office, dot your i's.") == []


def test_mailto_links_and_html_entities_are_handled():
    html = '<a href="mailto:recruiting@acme.com">Email us</a> or info&#64;acme.com'
    assert set(emails(html)) == {"recruiting@acme.com", "info@acme.com"}


@pytest.mark.parametrize(
    "junk",
    [
        "logo@2x.png", "noreply@acme.com", "someone@example.com", "abc123@sentry.io",
        "firstname.lastname@company.com", "user@domain.com",
        "0123456789abcdef0123@sentry-next.wixpress.com",
    ],
)
def test_placeholders_tooling_and_assets_are_rejected(junk):
    assert emails(f"Contact {junk} for details") == []


def test_addresses_are_deduplicated_case_insensitively():
    assert emails("HR@Acme.com and hr@acme.com") == ["hr@acme.com"]


def test_hiring_mailboxes_rank_ahead_of_general_ones():
    text = "General: info@acme.com. Applications: careers@acme.com. Or hello@acme.com."
    assert emails(text)[0] == "careers@acme.com"


# ==============================================================================
# CLASSIFICATION
# ==============================================================================

@pytest.mark.parametrize(
    "email,kind",
    [
        ("careers@acme.com", "hiring"), ("hr@acme.com", "hiring"), ("talent.acquisition@acme.com", "hiring"),
        ("jobs.india@acme.com", "hiring"), ("recruitment2024@acme.com", "hiring"),
        ("priya.sharma@acme.com", "person"), ("rahul@acme.com", "person"),
        ("info@acme.com", "general"), ("hello@acme.com", "general"),
        ("x7@acme.com", "other"),
    ],
)
def test_classification(email, kind):
    assert classify_email(email) == kind


@pytest.mark.parametrize(
    "host,expected",
    [
        ("https://careers.acme.com/jobs", "acme.com"), ("www.acme.co.in", "acme.co.in"),
        ("jobs.acme.co.uk", "acme.co.uk"), ("acme.io", "acme.io"), (None, ""),
    ],
)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


# ==============================================================================
# JOB POSTING STORAGE
# ==============================================================================

def _job(**overrides):
    from job_agent.config.schema import JobPosting

    data = dict(id="c0001", title="APM", company="Acme", job_url="https://www.linkedin.com/jobs/view/1",
                source="linkedin")
    data.update(overrides)
    return JobPosting(**data)


def test_postings_without_contacts_remain_valid():
    """Artifacts written before contacts existed must still load."""
    job = _job()
    assert job.contacts == [] and job.apply_url is None and job.primary_contact() is None


def test_contacts_are_deduplicated_keeping_the_most_direct_source():
    job = _job(contacts=[
        {"email": "HR@acme.com", "kind": "hiring", "source": "company_site", "source_url": "https://acme.com/careers"},
        {"email": "hr@acme.com", "kind": "hiring", "source": "job_post"},
    ])
    assert len(job.contacts) == 1
    assert job.contacts[0].source == "job_post"


def test_primary_contact_prefers_a_recruiting_mailbox():
    job = _job(contacts=[
        {"email": "info@acme.com", "kind": "general", "source": "company_site"},
        {"email": "careers@acme.com", "kind": "hiring", "source": "company_site"},
    ])
    assert job.primary_contact().email == "careers@acme.com"


def test_a_contact_must_record_its_source():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _job(contacts=[{"email": "hr@acme.com", "kind": "hiring"}])


def test_apply_url_is_normalised_and_invalid_values_dropped():
    assert _job(apply_url="jobs.lever.co/acme/1/apply").apply_url == "https://jobs.lever.co/acme/1/apply"
    assert _job(apply_url="not a url").apply_url is None
