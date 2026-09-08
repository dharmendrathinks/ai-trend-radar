"""Explicit researcher feedback; never a learned ranking input or a precision claim."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import re

from ai_trend_radar.models import isoformat


FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS brief_presentations (
    event_id TEXT NOT NULL REFERENCES radar_events(event_id), revision INTEGER NOT NULL,
    presented_at TEXT NOT NULL, PRIMARY KEY(event_id,revision)
);
CREATE TABLE IF NOT EXISTS research_feedback (
    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES radar_events(event_id),
    revision INTEGER NOT NULL, scan_id TEXT, verdict TEXT NOT NULL,
    already_known TEXT NOT NULL, recorded_at TEXT NOT NULL, note TEXT,
    source_families_json TEXT NOT NULL, discovery_origins_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_feedback_event ON research_feedback(event_id,revision,id);
"""


def record_feedback(database, event_id: str, verdict: str, known: str, now: datetime, note: str | None = None, revision: int | None = None) -> str:
    if verdict not in {'investigate', 'brief', 'skip'} or known not in {'yes', 'no', 'unknown'}:
        raise ValueError('choose investigate/brief/skip and --known yes/no/unknown')
    if not re.fullmatch(r'[0-9a-f]{4,64}', event_id):
        raise ValueError('use the event ID or an unambiguous prefix of at least four characters')
    with database.connect() as cx:
        cx.execute('BEGIN IMMEDIATE')
        rows = cx.execute('SELECT * FROM radar_events WHERE event_id LIKE ? AND revision>0', (event_id + '%',)).fetchall()
        if len(rows) != 1:
            raise ValueError('event ID is unknown or ambiguous')
        row = rows[0]
        if revision is not None and revision != row['revision']:
            raise ValueError('event changed since this report; review the latest evidence before rating')
        payload = json.loads(row['payload_json'])
        families = sorted({i['source_family'] for i in payload.get('observed_signals', [])
                           if i.get('evidence_role', 'event') == 'event'})
        origins = sorted({i.get('metrics', {}).get('discovery_origin') or i.get('provider', i['source_family'])
                          for i in payload.get('observed_signals', []) if i.get('evidence_role', 'event') == 'event'})
        cx.execute('''INSERT INTO research_feedback(event_id,revision,scan_id,verdict,already_known,recorded_at,note,source_families_json,discovery_origins_json)
                      VALUES (?,?,?,?,?,?,?,?,?)''',
                   (row['event_id'], row['revision'], row['scan_id'], verdict, known, isoformat(now), note, json.dumps(families), json.dumps(origins)))
    return row['event_id']


def feedback_summary(database, scope: str = "all") -> dict:
    with database.connect() as cx:
        cx.execute('BEGIN')
        rows = cx.execute('''SELECT f.* FROM research_feedback f JOIN
                             (SELECT event_id,revision,MAX(id) id FROM research_feedback GROUP BY event_id,revision) latest
                             ON f.id=latest.id''').fetchall()
        presentations = {(r[0], r[1]) for r in cx.execute('SELECT event_id,revision FROM brief_presentations')}
        history_count = cx.execute('SELECT COUNT(*) FROM research_feedback').fetchone()[0]
        topic_ids = {r[0] for r in cx.execute('SELECT topic_id FROM development_topics')}
        if scope in {'topics', 'legacy'}:
            include = lambda key: (key in topic_ids) == (scope == 'topics')
            rows = [r for r in rows if include(r['event_id'])]
            presentations = {p for p in presentations if include(p[0])}
            history_count = sum(include(r[0]) for r in cx.execute('SELECT event_id FROM research_feedback'))
    judgments = Counter(r['verdict'] for r in rows)
    known = Counter(r['already_known'] for r in rows)
    rated = {(r['event_id'], r['revision']) for r in rows}
    useful_new = [r for r in rows if r['already_known'] == 'no' and r['verdict'] in {'investigate', 'brief'}]
    def grouped(column):
        sources: dict[str, dict] = {}
        for row in rows:
            for source in json.loads(row[column]):
                counts = sources.setdefault(source, {'rated_revisions': 0, 'useful_previously_unknown_revisions': 0})
                counts['rated_revisions'] += 1
                counts['useful_previously_unknown_revisions'] += row['already_known'] == 'no' and row['verdict'] in {'investigate', 'brief'}
        return sources
    return {
        'scope': f'{scope}: all-time explicit feedback; latest judgment per revision',
        'presented_revisions': len(presentations), 'rated_presented_revisions': len(presentations & rated),
        'unrated_presented_revisions': len(presentations - rated),
        'rated_revisions': len(rows), 'rated_unique_events': len({r['event_id'] for r in rows}),
        'feedback_history_count': history_count,
        'verdicts': {key: judgments[key] for key in ('investigate', 'brief', 'skip')},
        'already_known': {key: known[key] for key in ('yes', 'no', 'unknown')},
        'useful_previously_unknown_revisions': len(useful_new),
        'useful_previously_unknown_events': len({r['event_id'] for r in useful_new}),
        'by_source_family': grouped('source_families_json'),
        'by_discovery_origin': grouped('discovery_origins_json'),
        'limitations': 'Self-selected judgments, not precision, recall, or causal source contribution. Unrated and unknown awareness are not failures. Presentations start with this schema and mean brief output, not confirmed reading or Slack receipt. Source counts overlap for multi-source events.',
    }


def render_feedback_summary(summary: dict) -> str:
    verdicts = summary['verdicts']
    unit = 'developments' if summary['scope'].startswith('topics:') else 'events'
    return '\n'.join([
        f'Research feedback (latest judgment per {unit} revision)',
        f"Rated: {summary['rated_revisions']} revisions across {summary['rated_unique_events']} {unit}.",
        f"Investigate: {verdicts['investigate']} · Brief mention: {verdicts['brief']} · Skip: {verdicts['skip']}",
        f"Useful and previously unknown: {summary['useful_previously_unknown_events']} {unit} ({summary['useful_previously_unknown_revisions']} revisions).",
        f"Already known: {summary['already_known']['yes']} · Awareness unknown: {summary['already_known']['unknown']}",
        f"Brief coverage: {summary['rated_presented_revisions']}/{summary['presented_revisions']} presented revisions rated; {summary['unrated_presented_revisions']} unrated.",
        summary['limitations'],
    ])
