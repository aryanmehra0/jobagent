"""Phase seven: interview questions with exact-source STAR draft evidence."""
from __future__ import annotations

import hashlib
import re

from job_agent.config.schema import InterviewPrep, InterviewQuestion
from job_agent.config.settings import settings
from job_agent.generation import evidence_for, verified_evidence, safe_id, write_json
from job_agent.intake.validator import load_and_verify_profile
from job_agent.runtime import exclusive_run, check_cancelled
from job_agent.tailoring.pipeline import ResumeTailoringPipeline


def prepare(profile, job, *, use_llm=True):
    evidence = verified_evidence(profile, evidence_for(profile, job, use_llm=use_llm))
    requirements = [line.strip(' -\t') for line in re.split(r'[\n.!?]+', job.description)
                    if len(line.strip()) > 25]
    requirements = sorted(requirements, key=lambda line: not bool(re.search(
        r'requir|experience|responsib|skill|must|develop|design', line, re.I)))[:4]
    questions = []
    for index in range(4):
        topic = requirements[index % len(requirements)][:240] if requirements else job.title
        questions.append(InterviewQuestion(bucket="technical", question=f"How would you approach this stated role requirement: {topic}?"))
    for index, question in enumerate(("Describe a difficult problem you solved.",
                                      "Tell me about a decision or tradeoff in your work.",
                                      "Describe an achievement and your personal contribution.")):
        entry = evidence[index % len(evidence)] if evidence else None
        star = {"Situation": f"{entry['title']} at {entry['company']}" if entry and entry['company'] else "[Add the verified context]",
                "Task": "[Explain your responsibility; the resume does not separate it from the action]",
                "Action": entry['statement'] if entry else "[No supporting achievement is recorded]",
                "Result": "[Explain the result using only the quoted evidence; do not invent an outcome]"}
        questions.append(InterviewQuestion(bucket="behavioral", question=question, star=star,
                                            evidence=[entry['statement']] if entry else []))
    questions.extend(InterviewQuestion(bucket="company-fit", question=text) for text in (
        f"Why does the {job.title} role at {job.company} interest you?",
        "Which stated requirement is your strongest fit, and which is a learning gap?",
        "What would you clarify before accepting this role?"))
    return InterviewPrep(job_id=job.id, company=job.company, title=job.title,
                         profile_hash=profile.profile_hash, questions=questions, emphasis=requirements,
                         panel_hypothesis="Possible recruiter, hiring manager and functional peer. This is a hypothesis, not confirmed company information.",
                         questions_to_ask=["What outcomes would define success in this role?",
                                           "What is the team's most difficult current problem?",
                                           "How are priorities and feedback handled on this team?"])


def markdown(prep):
    lines = [f"# Interview preparation: {prep.title} — {prep.company}",
             "Draft for practice. Quoted evidence is from your sealed profile; complete bracketed prompts yourself.",
             "## Company and role briefing", "Based only on the job description; company facts have not been independently researched."]
    lines += [f"- {item}" for item in prep.emphasis] or ["No detailed requirements available."]
    lines += ["", prep.panel_hypothesis, "", "### Questions to ask"]
    lines += [f"- {q}" for q in prep.questions_to_ask]
    for bucket in ("technical", "behavioral", "company-fit"):
        lines += ["", f"## {bucket.title()} questions"]
        for question in prep.questions:
            if question.bucket != bucket:
                continue
            lines += ["", f"### {question.question}"]
            lines += [f"**{key}:** {value}" for key, value in question.star.items()]
    return "\n\n".join(lines) + "\n"


class InterviewPrepPipeline:
    @exclusive_run
    def run(self, *, job_id=None, limit=None, use_llm=True):
        profile, valid = load_and_verify_profile(settings.profile_path)
        if not valid:
            raise ValueError("Profile seal failed; re-run intake before preparing answers.")
        jobs = ResumeTailoringPipeline._load_qualified(settings.outputs_dir / 'qualified_jobs.json')
        jobs = sorted((j for j in jobs if job_id is None or j.job.id == job_id), key=lambda j: -j.evaluation.fit_score)
        if job_id is not None and not jobs:
            raise ValueError('This job is not in the current qualified list. Re-evaluate it before preparing a guide.')
        folder = settings.outputs_dir / 'interview_prep'
        folder.mkdir(parents=True, exist_ok=True)
        from job_agent.tracking.export import _read_json
        records = {item['job_id']: item for item in _read_json(folder/'manifest.json', [])
                   if item.get('profile_hash') == profile.profile_hash}
        generated = []
        for item in jobs[:limit]:
            check_cancelled()
            prep = prepare(profile, item.job, use_llm=use_llm)
            path = folder / (safe_id(prep.job_id) + '.md')
            temporary = path.with_suffix('.md.tmp')
            temporary.write_text(markdown(prep), encoding='utf-8')
            temporary.replace(path)
            write_json(path.with_suffix('.json'), prep.model_dump())
            record = {'job_id': prep.job_id, 'profile_hash': prep.profile_hash, 'path': str(path),
                      'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'questions': len(prep.questions)}
            records[prep.job_id] = record
            generated.append(record)
            write_json(folder/'manifest.json', list(records.values()))
        return generated
