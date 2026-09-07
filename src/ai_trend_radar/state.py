"""Small SQLite occurrence ledger and review inbox; no story graph or scoring model."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Any
from urllib.parse import urlsplit
import json
import re

from ai_trend_radar.db import Database
from ai_trend_radar.models import Candidate, SourceItem, isoformat
from ai_trend_radar.resolution import occurrence_key
from ai_trend_radar.utils import normalize_url

STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS radar_events (
    event_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL DEFAULT '{}',
    revision INTEGER NOT NULL DEFAULT 0, briefed_revision INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT, interest_band TEXT, disposition TEXT, scan_id TEXT,
    change_reason TEXT, decision TEXT NOT NULL DEFAULT 'open',
    decision_revision INTEGER NOT NULL DEFAULT 0, deferred_until TEXT
);
CREATE TABLE IF NOT EXISTS event_keys (
    source_key TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES radar_events(event_id)
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES radar_events(event_id),
    action TEXT NOT NULL, revision INTEGER NOT NULL, decided_at TEXT NOT NULL,
    deferred_until TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS growth_checkpoints (
    source_key TEXT PRIMARY KEY, measured_at TEXT NOT NULL, stars REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS growth_events (
    event_key TEXT PRIMARY KEY, occurred_at TEXT NOT NULL, payload_json TEXT NOT NULL
);
"""


def source_keys(item: SourceItem) -> set[str]:
    keys = {occurrence_key(item)}
    if item.item_type == 'github_observed_growth':
        return keys
    if item.item_type == 'github_release' and item.metrics.get('release_id') is not None:
        keys.add(f"github:release-id:{item.metrics['release_id']}")
    url = normalize_url(item.canonical_url)
    parts = urlsplit(url)
    segments = [p for p in parts.path.split('/') if p]
    project_page = parts.hostname in {'github.com', 'huggingface.co'} and len(segments) <= 2
    if url and segments and not project_page:
        keys.add(f'permalink:{url}')
    return keys


class RadarState:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _existing_ids(cx, item: SourceItem) -> set[str]:
        # A native/source identity wins over a conflicting target URL.
        direct = cx.execute('SELECT event_id FROM event_keys WHERE source_key=?', (occurrence_key(item),)).fetchone()
        if direct:
            return {direct[0]}
        ids = {row[0] for key in source_keys(item) for row in cx.execute('SELECT event_id FROM event_keys WHERE source_key=?', (key,))}
        native = item.metrics.get('release_id') if item.item_type == 'github_release' else None
        if native is not None:
            ids = {event_id for event_id in ids if not any(
                row[0] != f'release-id:{native}' for row in cx.execute(
                    "SELECT source_key FROM event_keys WHERE event_id=? AND source_key LIKE 'release-id:%'", (event_id,)))}
        return ids

    def annotate(self, items: list[SourceItem]) -> None:
        with self.database.connect() as cx:
            for item in items:
                item.metrics.pop('persisted_event_id', None)
                ids = self._existing_ids(cx, item)
                if len(ids) == 1:
                    item.metrics['persisted_event_id'] = ids.pop()

    def bind(self, candidates: list[Candidate]) -> None:
        with self.database.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            for candidate in candidates:
                keys = set().union(*(source_keys(i) for i in candidate.items if i.evidence_role == 'event'))
                ids = set().union(*(self._existing_ids(cx, i) for i in candidate.items if i.evidence_role == 'event'))
                if len(ids) > 1:
                    raise ValueError('conflicting persisted event identities; refusing to merge review state')
                event_id = next(iter(ids), candidate.fingerprint)
                cx.execute('INSERT OR IGNORE INTO radar_events(event_id) VALUES (?)', (event_id,))
                for key in sorted(keys):
                    cx.execute('INSERT OR IGNORE INTO event_keys(source_key,event_id) VALUES (?,?)', (key, event_id))
                candidate.fingerprint = event_id

    def growth_events(self, items: list[SourceItem], config, now: datetime) -> list[SourceItem]:
        """Emit disjoint, measured intervals. Replays and unchanged counters cannot re-emit."""
        gate = config.ranking.eligibility
        with self.database.connect() as cx:
            cx.execute('BEGIN IMMEDIATE')
            for item in items:
                if item.item_type != 'github_repository_snapshot':
                    continue
                if item.cache_state not in {'live', 'validated-cache'} or 'stars' not in item.metrics:
                    continue
                key = occurrence_key(item)
                at, stars = item.measurement_time, float(item.metrics['stars'])
                old = cx.execute('SELECT measured_at,stars FROM growth_checkpoints WHERE source_key=?', (key,)).fetchone()
                if old is None:
                    cx.execute('INSERT INTO growth_checkpoints VALUES (?,?,?)', (key, isoformat(at), stars))
                    continue
                begin = datetime.fromisoformat(old[0].replace('Z', '+00:00'))
                hours = (at - begin).total_seconds() / 3600
                delta = stars - old[1]
                relative = delta / old[1] * 100 if old[1] > 0 else (100 if delta > 0 else 0)
                if hours < gate.watched_repo_growth_min_observation_hours or delta <= 0 or delta < gate.watched_repo_growth_min_star_delta or relative < gate.watched_repo_growth_min_relative_percent:
                    continue
                # Repository creation is already a separate occurrence during its lookback.
                if item.published_at and now - item.published_at <= timedelta(days=config.lookback_days):
                    continue
                event_key = f'{item.external_id}:growth:{isoformat(at)}'
                growth = {'available': True, 'first_observed_at': isoformat(begin),
                          'observation_duration_hours': round(hours, 3),
                          'metrics': {'stars': {'initial': old[1], 'current': stars, 'delta': delta}}}
                event = replace(item, external_id=event_key, item_type='github_observed_growth',
                                title=f"{item.metrics.get('repo_full_name', item.title)}: observed star growth (+{int(delta)} over {hours:.1f}h)",
                                published_at=at, updated_at=None, observed_at=at, first_seen_at=at,
                                metrics={**item.metrics, 'observed_growth': growth, 'growth_interval_start': isoformat(begin),
                                         'growth_interval_end': isoformat(at), 'event_basis': 'observed growth interval ending',
                                         'observed_star_relative_percent': round(relative, 3)})
                event.metrics.pop('persisted_event_id', None)
                cx.execute('INSERT OR IGNORE INTO growth_events VALUES (?,?,?)', (event_key, isoformat(at), json.dumps(event.to_dict())))
                cx.execute('UPDATE growth_checkpoints SET measured_at=?,stars=? WHERE source_key=?', (isoformat(at), stars, key))
            rows = cx.execute('SELECT payload_json FROM growth_events WHERE occurred_at>=?', (isoformat(now - timedelta(days=config.lookback_days)),)).fetchall()
        return [SourceItem.from_dict(json.loads(row[0])) for row in rows]

    def save_candidates(self, candidates: list[tuple[dict[str, Any], str]], scan_id: str) -> None:
        levels = {'early/limited': 0, 'moderate': 1, 'strong': 2}
        with self.database.connect() as cx:
            for payload, disposition in candidates:
                payload = dict(payload)
                payload.pop('youtube_evidence', None)  # The inbox does not duplicate YouTube responses.
                event_id = payload['fingerprint']
                signals = [i for i in payload['observed_signals'] if i.get('evidence_role', 'event') == 'event']
                primary = [i for i in signals if i.get('authority') == 'official'] or signals[:1]
                content = sorted((i['provider'], i['external_id'], re.sub(r'\s+', ' ', i.get('full_text') if i.get('full_text') is not None else i['summary']).strip(), i['title']) for i in primary)
                digest = sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()
                old = cx.execute('SELECT * FROM radar_events WHERE event_id=?', (event_id,)).fetchone()
                if old is None:
                    raise ValueError(f'event {event_id} has not been bound')
                reasons = []
                if old['revision'] == 0:
                    reasons.append('new discovery')
                else:
                    if old['content_hash'] != digest:
                        reasons.append('source text changed')
                    if levels.get(payload['interest_band'], 0) > levels.get(old['interest_band'], 0):
                        reasons.append('event interest increased')
                    if disposition == 'main' and old['disposition'] != 'main':
                        reasons.append('now qualifies for the main list')
                revision = old['revision'] + bool(reasons)
                cx.execute('''UPDATE radar_events SET payload_json=?, revision=?, content_hash=?, interest_band=?,
                              disposition=?,scan_id=?,change_reason=? WHERE event_id=?''',
                           (json.dumps(payload, ensure_ascii=False), revision, digest, payload['interest_band'], disposition,
                            scan_id, '; '.join(reasons) if reasons else old['change_reason'], event_id))

    def brief(self, now: datetime, top: int, scan_id: str | None = None) -> dict[str, Any]:
        with self.database.connect() as cx:
            if scan_id is None:
                last = cx.execute('SELECT scan_id FROM scans ORDER BY completed_at DESC LIMIT 1').fetchone()
                scan_id = last[0] if last else None
            rows = cx.execute('SELECT * FROM radar_events WHERE revision>0').fetchall()
            scan = cx.execute('SELECT provider_status_json FROM scans WHERE scan_id=?', (scan_id,)).fetchone()
        sections: dict[str, list] = {'new': [], 'updated': [], 'due': []}
        for row in rows:
            deferred = row['decision'] == 'deferred'
            if deferred and datetime.fromisoformat(row['deferred_until'].replace('Z', '+00:00')) > now:
                continue
            if not deferred and (row['scan_id'] != scan_id or row['disposition'] != 'main'):
                continue
            if not deferred and row['decision'] != 'reopen':
                if row['briefed_revision'] >= row['revision']:
                    continue
                if row['decision'] == 'reviewed' and row['decision_revision'] >= row['revision']:
                    continue
            section = 'due' if deferred else ('new' if row['briefed_revision'] == 0 and row['decision'] != 'reopen' else 'updated')
            payload = json.loads(row['payload_json'])
            sections[section].append({'event_id': row['event_id'], 'revision': row['revision'],
                                      'reason': 'deferral due' if deferred else row['change_reason'],
                                      'last_scan_id': row['scan_id'], 'current': row['scan_id'] == scan_id,
                                      'disposition': row['disposition'], 'decision': row['decision'],
                                      'deferred_until': row['deferred_until'], 'candidate': payload})
        for name, values in sections.items():
            values.sort(key=lambda x: (-x['candidate']['discovery_priority'], x['event_id']))
            sections[name] = values[:top if name == 'new' else 3]
        return {'scan_id': scan_id, 'generated_at': isoformat(now), 'provider_status': json.loads(scan[0]) if scan else [], **sections}

    def acknowledge(self, brief: dict[str, Any]) -> None:
        with self.database.connect() as cx:
            for name in ('new', 'updated', 'due'):
                for item in brief[name]:
                    cx.execute('INSERT OR IGNORE INTO brief_presentations(event_id,revision,presented_at) VALUES (?,?,?)',
                               (item['event_id'], item['revision'], brief['generated_at']))
                    cx.execute("UPDATE radar_events SET briefed_revision=MAX(briefed_revision,?) WHERE event_id=? AND revision=?",
                               (item['revision'], item['event_id'], item['revision']))
                    if item.get('decision') in {'deferred', 'reopen'}:
                        # Do not erase a decision made while output was being written.
                        cx.execute("""UPDATE radar_events SET decision='open', deferred_until=NULL
                                      WHERE event_id=? AND revision=? AND decision=? AND deferred_until IS ?""",
                                   (item['event_id'], item['revision'], item['decision'], item.get('deferred_until')))

    def decide(self, event_id: str, action: str, now: datetime, until: datetime | None = None, note: str | None = None) -> str:
        if action not in {'reviewed', 'deferred', 'reopen'}:
            raise ValueError('decision must be reviewed, deferred, or reopen')
        if until is not None and until.tzinfo is None:
            raise ValueError('deferral timestamp must include a timezone')
        if action == 'deferred' and (until is None or until <= now):
            raise ValueError('deferred requires --until with a future timezone-aware timestamp')
        if action != 'deferred' and until is not None:
            raise ValueError('--until is only valid for deferred')
        if not re.fullmatch(r'[0-9a-f]{4,64}', event_id):
            raise ValueError('use the event ID or an unambiguous prefix of at least four characters')
        with self.database.connect() as cx:
            rows = cx.execute('SELECT event_id,revision FROM radar_events WHERE event_id LIKE ? AND revision>0', (event_id + '%',)).fetchall()
            if len(rows) != 1:
                raise ValueError('event ID is unknown or ambiguous')
            key, revision = rows[0]
            cx.execute('UPDATE radar_events SET decision=?,decision_revision=?,deferred_until=? WHERE event_id=?', (action, revision, isoformat(until), key))
            cx.execute('INSERT INTO decisions(event_id,action,revision,decided_at,deferred_until,note) VALUES (?,?,?,?,?,?)', (key, action, revision, isoformat(now), isoformat(until), note))
        return key


def render_brief(brief: dict[str, Any]) -> str:
    lines = ['# AI Trend Radar — Changes', '', f"Generated: {brief['generated_at']} · Scan: {brief['scan_id'] or 'none'}", '']
    problems = [p for p in brief.get('provider_status', []) if p['status'] in {'failed', 'partial', 'stale'}]
    if problems:
        lines.extend(['Collection gaps: ' + ', '.join(f"{p['provider']} ({p['status']})" for p in problems) + '. Discovery coverage is incomplete.', ''])
    for key, title in [('new', 'New discoveries'), ('updated', 'Material updates'), ('due', 'Deferred items due')]:
        lines.extend([f'## {title}', ''])
        if not brief[key]:
            lines.extend(['None.', ''])
        for item in brief[key]:
            c = item['candidate']
            lines.extend([f"### {c.get('display_title') or c['title']}", '',
                          f"Event: `{item['event_id']}` · {item['reason']}", '',
                          f"Event time: {c['event_time']} ({c.get('event_time_basis', 'source timestamp')}).", '',
                          f"Evidence: {c['evidence_level']}. Interest: {c['interest_band']} ({c['interest_rule']}).", ''])
            if not item['current']:
                lines.extend(['Not rechecked in this scan; inspect the source before acting.', ''])
            if item['disposition'] != 'main':
                lines.extend(['Reminder only; this item does not currently qualify for the main list.', ''])
            lines.extend(f'- {url}' for url in c['source_links'])
            lines.extend(['', f"Review: `ai-trend-radar decide {item['event_id']} reviewed`", '',
                          f"Feedback: `ai-trend-radar feedback {item['event_id']} investigate --known no --revision {item['revision']}` (or `brief` / `skip`; awareness `yes` / `no` / `unknown`).", '',
                          f"Defer: `ai-trend-radar decide {item['event_id']} deferred --until <ISO timestamp with timezone>`", ''])
    lines.extend(['Unchanged previously presented items are omitted. The full scan report retains discovery and watch lists.', ''])
    return '\n'.join(lines)
