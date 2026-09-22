"""Optional, concise cover letters assembled from exact candidate evidence."""
from __future__ import annotations
import hashlib
from pypdf import PdfReader

from job_agent.config.settings import settings
from job_agent.generation import evidence_for, verified_evidence, safe_id, write_json
from job_agent.tailoring.compiler import TypstResumeCompiler
from job_agent.tailoring.regional import regional_policy


def paragraphs(profile, job, *, use_llm=True):
    evidence = verified_evidence(profile, evidence_for(profile, job, use_llm=use_llm))
    chosen = []
    for entry in evidence:
        if len((' '.join(chosen) + ' ' + entry['statement']).split()) <= 120:
            chosen.append(entry['statement'])
        if len(chosen) == 2:
            break
    return ["I am applying for the position listed above. I would welcome the opportunity to discuss how my background relates to the role's stated requirements.",
            ' '.join(chosen) or "Please see my attached resume for my recorded background. I would be happy to discuss it in an interview.",
            "Thank you for reviewing my application. I would welcome a conversation about the team's priorities and the contribution this role calls for."]


class LetterCompiler(TypstResumeCompiler):
    def validate_ats_pdf(self, pdf_file, tailored_data):
        reader = PdfReader(str(pdf_file))
        text = '\n'.join(page.extract_text() or '' for page in reader.pages)
        required = tailored_data['paragraphs'] + [tailored_data['contact']['full_name']]
        valid = len(reader.pages) == 1 and all(self._search_key(s) in self._search_key(text) for s in required)
        if not valid:
            raise ValueError("Cover letter must be one readable page preserving all selected evidence.")
        return {'passed': True, 'page_count': 1, 'source_evidence_preserved': True}


def generate(profile, job, *, country=None, use_llm=True):
    safe_id(job.id)
    folder = settings.outputs_dir / 'cover_letters'
    data = {'contact': profile.contact.model_dump(mode='json'), 'role': job.title,
            'company': job.company, 'paper': regional_policy(job, country)['paper'],
            'paragraphs': paragraphs(profile, job, use_llm=use_llm)}
    compiler = LetterCompiler(settings.templates_dir/'cover_letter.typ', folder)
    pdf = compiler.compile_resume(data, job.id)
    target = folder / f'cover_{job.id}.pdf'
    pdf.replace(target)
    record = {'job_id': job.id, 'profile_hash': profile.profile_hash, 'path': str(target),
              'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'validated': True}
    from job_agent.tracking.export import _read_json
    manifest = {i['job_id']: i for i in _read_json(folder/'manifest.json', [])
                if i.get('profile_hash') == profile.profile_hash}
    manifest[job.id] = record
    write_json(folder/'manifest.json', list(manifest.values()))
    return record
