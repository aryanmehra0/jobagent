"""Outcome analytics from observed events, never from draft counts."""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from statistics import median

from job_agent.config.settings import settings
from job_agent.tracking.export import JobsCsvExporter, _read_json


def analytics():
    rows = list(JobsCsvExporter().load().values())
    ledger_path = settings.outputs_dir/'inbox_events.json'
    events = _read_json(ledger_path, {}).get('events', [])
    manual = _read_json(settings.outputs_dir/'manual_applications.json', {})
    history = defaultdict(set)
    first = {}
    for event in events:
        history[event['job_id']].add(event['status'])
        if event.get('received_at'):
            first[event['job_id']] = min(first.get(event['job_id'], event['received_at']), event['received_at'])
    counts = dict.fromkeys(('sourced','qualified','tailored','applied','replied','interview','offer'), 0)
    buckets, sources, variants, roles = (defaultdict(lambda: {'applied': 0, 'replied': 0}) for _ in range(4))
    delays = []
    for row in rows:
        jid, status = row['Job ID'], row.get('Status', '')
        try:
            score = float(row.get('Fit Score') or -1)
        except ValueError:
            score = -1
        replied = bool(history[jid]) or status.startswith('replied_')
        applied = status == 'applied' or replied
        tailored = bool(row.get('Tailored Resume'))
        counts['sourced'] += 1
        counts['qualified'] += score >= settings.min_match_score or tailored
        counts['tailored'] += tailored
        counts['applied'] += applied
        counts['replied'] += replied
        counts['interview'] += 'replied_interview' in history[jid] or status == 'replied_interview'
        counts['offer'] += 'replied_offer' in history[jid] or status == 'replied_offer'
        if applied:
            bucket = f'{min(9, int(score))}–{min(10, int(score)+1)}' if score >= 0 else 'Unscored'
            for group, key in ((buckets, bucket), (sources, row.get('Source') or 'Unknown'),
                               (variants, row.get('Resume Format') or 'Unknown'), (roles, row.get('Title') or 'Unknown')):
                group[key]['applied'] += 1
                group[key]['replied'] += replied
            sent = row.get('Applied At') or manual.get(jid, {}).get('at')
            if sent and jid in first:
                try:
                    days = (datetime.fromisoformat(first[jid])-datetime.fromisoformat(sent)).total_seconds()/86400
                    if days >= 0:
                        delays.append(days)
                except (ValueError, TypeError):
                    pass
    def rates(group):
        return [{'label': key, **value, 'response_rate': round(value['replied']/value['applied'], 4)} for key, value in sorted(group.items())]
    stages = []
    previous = None
    for name, count in counts.items():
        stages.append({'stage': name, 'count': count,
                       'conversion': count/previous if previous and count <= previous else None})
        previous = count
    return {'funnel': stages, 'by_score': rates(buckets), 'by_source': rates(sources),
            'by_variant': rates(variants), 'by_role': rates(roles),
            'median_response_days': median(delays) if delays else None, 'timed_responses': len(delays),
            'reply_tracking': ledger_path.exists(),
            'note': 'Observed outcomes only. Stages can be skipped; unavailable conversion rates are shown as unknown. Historical timestamps may be missing.' if ledger_path.exists() else
                    'Inbox tracking is not configured or has not been synced. Only preparation and applied counts are available; response metrics are unknown.'}
