"""Grounding, privacy, isolation and observed-outcome regression tests."""
import csv
import json
import re
import threading
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock

import pytest

from job_agent.config.settings import settings
from job_agent.config.schema import JobPosting
from tests.test_tailoring import candidate_profile, qualified_job


def job(**changes):
    values = dict(id='roadmap', title='Python Engineer', company='Acme',
                  job_url='https://acme.test/jobs/engineer', source='greenhouse',
                  description='Requirements: design Python services. Build reliable APIs and collaborate across teams.')
    return JobPosting(**(values | changes))


def test_interview_answers_reject_model_claims(monkeypatch, candidate_profile):
    from job_agent.interview.pipeline import prepare, markdown
    monkeypatch.setattr('job_agent.generation.complete_json', lambda *a: {'indices':[999, -1, '0', 0], 'answer': 'I grew revenue 99999% at Invented Inc.'})
    result = prepare(candidate_profile, job())
    assert len(result.questions) == 10
    assert {q.bucket for q in result.questions} == {'technical', 'behavioral', 'company-fit'}
    answers = ' '.join(' '.join(q.star.values()) for q in result.questions)
    assert '99999' not in answers and 'Invented' not in answers
    assert set(re.findall(r'\d+', answers)) <= set(re.findall(r'\d+', candidate_profile.model_dump_json()))
    assert 'hypothesis' in markdown(result)


def test_cover_letter_exact_evidence_one_page(monkeypatch, candidate_profile):
    from job_agent.tailoring.cover_letter import generate, paragraphs
    from pypdf import PdfReader
    result = generate(candidate_profile, job(), use_llm=False)
    assert result['validated']
    assert len(PdfReader(result['path']).pages) == 1
    assert len(' '.join(paragraphs(candidate_profile, job(), use_llm=False)).split()) < 200
    from job_agent.tracking.supplements import document_links
    assert document_links(profile_hash=candidate_profile.profile_hash)['roadmap']['Cover Letter']
    Path(result['path']).write_bytes(b'corrupt')
    assert not document_links(profile_hash=candidate_profile.profile_hash)


@pytest.mark.parametrize('text,status', [
    ('Unfortunately your application is not moving forward', 'replied_rejection'),
    ('Please schedule a call for an interview', 'replied_interview'),
    ('We are pleased to offer you employment', 'replied_offer'),
    ('Our company offers software products', 'replied_other'),
    ('Unfortunately we cannot schedule a call tomorrow', 'replied_other'),
    ('Unfortunately your application is still under review', 'replied_other'),
])
def test_reply_classification(text, status):
    from job_agent.tracking.inbox import classify
    assert classify(text) == status


def test_reply_matching_requires_unambiguous_applied_role():
    from job_agent.tracking.inbox import match_application
    row = {'Job ID':'one','Company':'Acme','Title':'Python Engineer', 'Status':'applied', 'Company Website':'https://acme.test'}
    assert match_application('Hiring <hr@acme.test>', 'Acme Python Engineer interview', [row]) == 'one'
    assert match_application('hr@outsider.test', 'Acme Python Engineer interview', [row]) is None
    assert match_application('hr@acme.test', 'Acme Python Engineer', [row, row | {'Job ID':'two'}]) is None
    assert match_application('hr@acme.test', 'Acme Python Engineer', [row | {'Status':'dry_run'}]) is None


def test_readonly_inbox_idempotent_and_csv_survives(monkeypatch):
    from job_agent.tracking.inbox import sync_inbox
    from job_agent.tracking.export import JobsCsvExporter
    for key, value in {'IMAP_HOST':'mail.acme.test','IMAP_USER':'candidate','IMAP_APP_PASSWORD':'test-secret','IMAP_FOLDER':'Applications'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('INBOX_USE_LLM', raising=False)
    exporter = JobsCsvExporter()
    exporter._write([{'Job ID':'one', 'Company':'Acme', 'Title':'Python Engineer','Status':'applied','Company Website':'https://acme.test'}])
    message = EmailMessage()
    message['From'] = 'hr@acme.test'
    message['Subject'] = 'Acme Python Engineer interview'
    message['Date'] = 'Mon, 21 Sep 2026 10:00:00 +0000'
    message.set_content('Please schedule a call for an interview.')
    calls = []
    class Mailbox:
        def login(self, *args): pass
        def select(self, folder, readonly):
            assert folder == 'Applications' and readonly is True
            return 'OK', []
        def response(self, key): return key, [b'123']
        def uid(self, command, *args):
            calls.append((command, args))
            return ('OK',[b'10']) if command == 'search' else ('OK',[(b'', message.as_bytes())])
        def logout(self): pass
    factory = lambda *a, **k: Mailbox()
    assert sync_inbox(client_factory=factory)['matched'] == 1
    assert sync_inbox(client_factory=factory)['matched'] == 0
    assert len([c for c in calls if c[0] == 'fetch']) == 1
    assert 'BODY.PEEK' in calls[1][1][1]
    assert exporter.load()['one']['Status'] == 'replied_interview'
    assert 'test-secret' not in (settings.outputs_dir/'inbox_events.json').read_text()


def test_inbox_off_without_explicit_folder(monkeypatch):
    from job_agent.tracking.inbox import sync_inbox
    monkeypatch.delenv('IMAP_FOLDER', raising=False)
    factory = Mock()
    with pytest.raises(ValueError, match='off'):
        sync_inbox(client_factory=factory)
    factory.assert_not_called()


def test_analytics_does_not_count_drafts_as_applications():
    from job_agent.web.analytics import analytics
    from job_agent.tracking.export import JobsCsvExporter
    JobsCsvExporter()._write([
        {'Job ID':'one','Status':'dry_run','Fit Score':'9.0','Source':'LinkedIn','Tailored Resume':'one.pdf'},
        {'Job ID':'two','Status':'replied_interview','Fit Score':'7.5','Source':'Lever','Applied At':'2026-09-19T10:00:00+00:00'}])
    (settings.outputs_dir/'inbox_events.json').write_text(json.dumps({'events':[{'job_id':'two','status':'replied_interview','received_at':'2026-09-21T10:00:00+00:00'}]}))
    result = analytics()
    funnel = {r['stage']:r['count'] for r in result['funnel']}
    assert funnel['applied'] == 1 and funnel['interview'] == 1
    assert result['median_response_days'] == 2
    assert result['by_source'] == [{'label':'Lever','applied':1,'replied':1,'response_rate':1.0}]


def test_public_people_require_explicit_visible_pairs():
    from job_agent.contacts.warm import extract_people
    html = '''<div class="team-member"><b class="name">Jane Smith</b><span class="role">Engineering Manager</span></div>
    <div class="team-member"><b class="name">Unknown</b><span class="role">Engineer</span></div>'''
    assert extract_people(html, 'https://acme.test/about', 'Engineer')[0]['name'] == 'Jane Smith'
    assert not extract_people(html, 'https://linkedin.com/about', 'Engineer')
    assert not extract_people('<p>Jane Smith is great at engineering</p>', 'https://acme.test/team', 'Engineer')
    assert not extract_people(html.replace('Jane Smith','Customer Support'), 'https://acme.test/contact', 'Engineer')


def test_profile_gaps_find_older_evidence_without_substring_matches(candidate_profile):
    from job_agent.evaluation.gaps import missing_evidence
    data = candidate_profile.model_dump()
    data['experience'].append({'description_bullets':['Maintained Rust and Go services.']})
    assert missing_evidence(data, ['Rust','Golang','Go']) == ['Rust','Go']


def test_reranker_includes_all_roles_and_bullets(candidate_profile):
    from job_agent.evaluation.reranker import LLMReranker
    text = LLMReranker._profile_summary(candidate_profile)
    assert all(b in text for role in candidate_profile.experience for b in role.description_bullets)


def test_hosted_user_isolation_and_revocation(tmp_path):
    from job_agent.hosted.auth import HostedIdentityStore
    from job_agent.hosted.api import HostedApiServer, HostedApiHandler
    from job_agent.hosted.queue import HostedQueue
    from tests.test_hosted_control_plane import _request
    queue = HostedQueue(tmp_path/'hosted.db')
    identities = HostedIdentityStore(queue)
    alice, bob = identities.issue_key('alice'), identities.issue_key('bob')
    identities.put_job('alice', 'private', {'title':'Private job'})
    assert alice.encode() not in (tmp_path/'hosted.db').read_bytes()
    server = HostedApiServer(('127.0.0.1',0), HostedApiHandler, queue=queue, identities=identities)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        status, payload = _request(base+'/runs', 'POST', token=alice, body=json.dumps({'phases':['prep']}).encode())
        assert status == 202 and payload['run']['user_id'] == 'alice'
        assert _request(base+f"/runs/{payload['run']['id']}", token=bob)[0] == 404
        assert _request(base+'/runs', 'POST', token=bob, body=json.dumps({'user_id':'alice','phases':['prep']}).encode())[0] == 403
        assert _request(base+'/jobs',token=bob)[1] == {'jobs':[]}
        assert _request(base+'/jobs',token=alice)[1]['jobs'][0]['title'] == 'Private job'
        identities.revoke(alice.split('.')[0])
        assert _request(base+'/jobs',token=alice)[0] == 401
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('label',['Race','Veteran status','Disability','Gender'])
def test_workday_never_fills_sensitive_fields(candidate_profile,label):
    from job_agent.automation.form_filler import FormFiller
    locator = Mock()
    filler = FormFiller(candidate_profile, job(job_url='https://acme.wd5.myworkdayjobs.com/jobs/123'))
    assert not filler.fill_field({'locator':locator,'label':label,'type':'text','tag':'input'})
    locator.fill.assert_not_called()
    locator.select_option.assert_not_called()


def test_workday_route_assistance_is_explicit():
    from job_agent.automation.routing import route_application
    posting = job(job_url='https://acme.wd5.myworkdayjobs.com/jobs/123')
    assert not route_application(posting).automatable
    assert route_application(posting, assist_workday=True).channel == 'workday_assisted'
    assert route_application(job(job_url='https://myworkdayjobs.com.attacker.test/jobs/123'), assist_workday=True).channel != 'workday_assisted'


def test_tailor_and_prep_publish_verified_documents(monkeypatch, tmp_path, candidate_profile, qualified_job):
    from job_agent.web.runner import PipelineRunner
    from job_agent.tracking.export import JobsCsvExporter
    from zipfile import ZipFile
    profile_path = tmp_path/'profile.json'
    profile_path.write_text(candidate_profile.model_dump_json(),encoding='utf-8')
    monkeypatch.setattr(settings,'profile_path',profile_path)
    settings.outputs_dir.mkdir(parents=True)
    (settings.outputs_dir/'qualified_jobs.json').write_text(json.dumps([qualified_job.model_dump()]),encoding='utf-8')
    result = PipelineRunner().run_sync(['tailor','prep'], {'tailoring_mode':'regional','cover_letter':True})
    assert result['status'] == 'ok',result
    row = JobsCsvExporter().load()[qualified_job.job.id]
    assert Path(row['Interview Prep']).is_file() and Path(row['Cover Letter']).is_file()
    with ZipFile(settings.outputs_dir/'application_pack.zip') as archive:
        assert len([p for p in archive.namelist() if p.startswith('interview_prep/')]) == 1
        assert len([p for p in archive.namelist() if p.startswith('cover_letters/')]) == 1
    candidate_profile.summary += ' Changed profile.'
    candidate_profile.seal_profile()
    profile_path.write_text(candidate_profile.model_dump_json(),encoding='utf-8')
    JobsCsvExporter().export()
    assert not JobsCsvExporter().load()[qualified_job.job.id]['Interview Prep']


def test_prep_refuses_invalid_seal(monkeypatch,candidate_profile):
    from job_agent.interview.pipeline import InterviewPrepPipeline
    monkeypatch.setattr('job_agent.interview.pipeline.load_and_verify_profile',lambda path:(candidate_profile,False))
    with pytest.raises(ValueError,match='seal'):
        InterviewPrepPipeline().run()


def test_cover_letter_ignores_fabricated_model_prose(monkeypatch,candidate_profile):
    from job_agent.tailoring.cover_letter import paragraphs
    monkeypatch.setattr('job_agent.generation.complete_json',lambda *a:{'indices':['fake'],'letter':'I managed 99999 people at Invented Company.'})
    text = ' '.join(paragraphs(candidate_profile,job()))
    assert '99999' not in text and 'Invented Company' not in text


def test_ambiguous_team_card_names_are_dropped():
    from job_agent.contacts.warm import extract_people
    html = '<div class="team-member"><span class="name">Jane Smith</span><span class="role">Engineer</span></div><div class="team-member"><span class="name">Jane Smith</span><span class="role">Engineering Manager</span></div>'
    assert not extract_people(html,'https://acme.test/team','Engineer')
    assert not extract_people('<script type="application/ld+json">{"@type":"Person","name":"Jane Smith","jobTitle":"Engineer"}</script>','https://acme.test/team','Engineer')
