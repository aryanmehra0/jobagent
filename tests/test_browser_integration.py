"""Opt-in real Chromium test using an intercepted portal; sends no applications."""

import os
import pytest

from job_agent.automation.agent import AutoApplyAgent
from job_agent.config.schema import CandidateProfile, ContactInfo, JobPosting, SkillSet, WorkAuthorization
from job_agent.sourcing.delta_store import DeltaStore


@pytest.mark.skipif(os.environ.get("JOB_AGENT_BROWSER_TESTS") != "1",
                    reason="Set JOB_AGENT_BROWSER_TESTS=1 to exercise installed Chromium")
@pytest.mark.parametrize("kind", ["single", "multi", "missing_required"])
def test_browser_fills_uploads_and_confirms_local_application(tmp_path, kind):
    from playwright.sync_api import sync_playwright

    captured = []
    html = """<!doctype html><html><body>
    <form onsubmit="event.preventDefault(); window.capture({
      name: document.querySelector('#name').value,
      email: document.querySelector('#email').value,
      file: document.querySelector('#resume').files[0].name
    }); document.body.innerHTML = 'Your application has been received';">
    <label for="name">Full name</label><input id="name" required>
    <label for="email">Email</label><input id="email" type="email" required>
    <label for="resume">Resume</label><input id="resume" type="file" required>
    <button type="submit">Submit application</button>
    </form></body></html>"""
    if kind == "multi":
        import json
        html = """<html><body><p>Thanks for your interest</p>
        <form onsubmit='event.preventDefault(); document.body.innerHTML = window.nextPage'>
        <label for="name">Full name</label><input id="name" required>
        <button type="submit">Continue</button></form>
        <script>window.nextPage = """ + json.dumps(html) + ";</script></body></html>"
    elif kind == "missing_required":
        html = html.replace('<button type="submit">', '<label for="unknown">Required referral code</label><input id="unknown" required><button type="submit">')
    profile = CandidateProfile(contact=ContactInfo(
        full_name="Test Candidate", email="candidate@example.com",
    ), summary="Test engineer", work_authorization=WorkAuthorization(current_country="United States"),
        skills=SkillSet(languages=["Python"]), years_of_experience=0)
    profile.seal_profile()
    job = JobPosting(id="browser_test", title="Engineer", company="Example",
                     job_url="https://example.test/apply", source="greenhouse")
    # Upload bytes are sufficient here; real PDF compilation is covered by the
    # six-stage test. All browser traffic is intercepted before leaving Chromium.
    pdf = tmp_path / "resume.pdf"
    pdf.write_bytes(b"%PDF-1.4\n%%EOF")
    store = DeltaStore(tmp_path / "delta.db")
    store.mark_seen(job)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))
        context.expose_function("capture", lambda values: captured.append(values))

        class Session:
            def new_stealth_page(self):
                return context.new_page()

            def close(self):
                context.close()

        agent = AutoApplyAgent(session_manager=Session(), delta_store=store)
        try:
            outcome = agent.apply_to_job(profile, job, pdf, dry_run=False)
            if kind == "missing_required":
                assert outcome["status"] == "failed"
                assert "Required fields" in outcome["error"]
                assert captured == []
                return
            assert outcome["status"] == "applied"
            assert captured == [{"name": "Test Candidate", "email": "candidate@example.com",
                                 "file": "resume.pdf"}]
            assert store.status_counts() == {"applied": 1}
        finally:
            agent.close()
            browser.close()
