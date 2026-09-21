from job_agent.config.schema import JobPosting, WorkAuthorization
from job_agent.config.normalize import strip_html


def test_query_identified_jobs_are_distinct_but_tracking_is_ignored():
    first = JobPosting.create_id("https://stripe.com/jobs/search?gh_jid=123", "Stripe", "Engineer")
    second = JobPosting.create_id("https://stripe.com/jobs/search?gh_jid=456", "Stripe", "Engineer")
    tracked = JobPosting.create_id("https://stripe.com/jobs/search?utm_source=x&gh_jid=123&gh_src=y", "Stripe", "Engineer")
    assert first != second
    assert first == tracked


def test_escaped_ats_html_becomes_plain_text():
    text = strip_html("&lt;p&gt;Requirements&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Python &amp;amp; Go&lt;/li&gt;&lt;/ul&gt;")
    assert "<p>" not in text
    assert "<li>" not in text
    assert "Python & Go" in text


def test_eligibility_is_unknown_without_explicit_facts():
    auth = WorkAuthorization(current_country="United States")
    assert auth.is_authorized_in("United States") is None
    assert auth.requires_sponsorship is None
