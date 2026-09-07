from dataclasses import replace
from datetime import UTC, datetime, timedelta
from ai_trend_radar.models import SourceItem, ProviderResult
from ai_trend_radar.db import Database
from ai_trend_radar.resolution import cluster_items, effective_item_time
from ai_trend_radar.ranking import attach_repository_support, rank_candidates, evidence
from ai_trend_radar.topics import attach_video_topics

NOW = datetime(2026, 9, 1, tzinfo=UTC)

def release(tag='v1.2.3'):
    return SourceItem('github_watched', f'anthropics/claude-code@{tag}', 'github', 'github_release',
        f'Claude Code {tag}', 'Chore: update dependencies.',
        f'https://github.com/anthropics/claude-code/releases/tag/{tag}', NOW, None, NOW,
        entity='Anthropic', authority='official', metrics={'repo_full_name': 'anthropics/claude-code', 'release_tag': tag})

def test_release_identity_and_transitive_conflicts(config):
    a, b = release(), release('v1.2.4')
    candidates = cluster_items([a, b], config)
    assert len({c.fingerprint for c in candidates}) == 2
    bridge = replace(b, provider='hacker_news', external_id='123', source_family='hacker_news',
                     item_type='hacker_news_story', canonical_url=a.canonical_url, metrics={}, authority='community')
    assert len(cluster_items([a, b, bridge], config)) == 2
    b.title = a.title
    assert len(cluster_items([a, b], config)) == 2

def test_release_identity_survives_body_edit(config):
    a = release()
    before = cluster_items([a], config)[0].fingerprint
    a.summary = 'New background agents and GPT-6 support'
    assert cluster_items([a], config)[0].fingerprint == before

def test_project_growth_does_not_promote_release(config):
    a = release()
    candidate = cluster_items([a], config)[0]
    snapshot = replace(a, external_id='anthropics/claude-code', item_type='github_repository_snapshot',
                       authority='community', metrics={'repo_full_name': 'anthropics/claude-code',
                       'observed_growth': {'available': True, 'metrics': {'stars': {'delta': 100}}}})
    attach_repository_support([candidate], [snapshot])
    rank_candidates([candidate], config, NOW)
    attach_video_topics([candidate], config.topics)
    assert candidate.interest_band == 'early/limited'
    assert candidate.video_topic['topicability']['status'] == 'release_watch'

def test_repost_does_not_establish_independence(config):
    a = release()
    repost = replace(a, provider='hacker_news', external_id='123', source_family='hacker_news', authority='community', metrics={})
    label, _ = evidence(cluster_items([a, repost], config)[0])
    assert 'independently confirmed' not in label

def test_undated_item_uses_first_seen(config):
    db = Database(config.database_path); db.initialize()
    a = replace(release(), published_at=None, updated_at=None)
    db.record_provider_result(ProviderResult(a.provider, 'ok', [a], NOW))
    b = replace(a, observed_at=NOW + timedelta(days=9))
    db.record_provider_result(ProviderResult(b.provider, 'ok', [b], b.observed_at))
    assert effective_item_time(b) == NOW


def test_growth_checkpoint_survives_restart_and_unchanged_counts(config):
    from ai_trend_radar.state import RadarState
    from ai_trend_radar.ranking import eligible_items, freshness_score
    db = Database(config.database_path); db.initialize()
    def observe(hours, stars, cache='live'):
        at = NOW + timedelta(hours=hours)
        item = replace(release(), external_id='anthropics/claude-code', item_type='github_repository_snapshot',
                       title='Claude Code AI coding agent', published_at=NOW-timedelta(days=500),
                       observed_at=at, cache_state=cache,
                       metrics={'repo_full_name': 'anthropics/claude-code', 'stars': stars})
        events = RadarState(db).growth_events([item], config, at)
        return eligible_items([item, *events], config, at)
    assert observe(0, 10000) == []
    first = observe(48, 10100)
    assert len(first) == 1
    later = observe(72, 10100)
    assert later[0].external_id == first[0].external_id
    assert effective_item_time(later[0]) == NOW + timedelta(hours=48)
    candidate = cluster_items(later, config)[0]
    assert freshness_score(candidate, config, NOW+timedelta(hours=72)) < 100
    assert len(observe(96, 10200, 'stale')) == 1
    assert len(observe(96, 10200)) == 2
    assert len(observe(96, 10200)) == 2
    assert observe(288, 10200) == []


def test_cached_and_stale_samples_not_inserted(config):
    db = Database(config.database_path); db.initialize()
    for hours, cache in [(0, 'live'), (24, 'cached'), (25, 'stale')]:
        at = NOW+timedelta(hours=hours)
        item = replace(release(), metrics={'stars': 100}, observed_at=at, cache_state=cache,
                       body_fetched_at=NOW, last_confirmed_at=NOW)
        db.record_provider_result(ProviderResult(item.provider, 'ok', [item], at))
    with db.connect() as cx:
        assert cx.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 1


def test_304_preserves_body_acquisition(config):
    import httpx
    import respx
    from unittest.mock import patch
    from ai_trend_radar.http import CachedHttpClient
    db = Database(config.database_path); db.initialize()
    client = CachedHttpClient(db, config.http)
    with respx.mock, patch('ai_trend_radar.http.datetime') as clock:
        clock.now.return_value = NOW
        route = respx.get('https://example.test/metrics').mock(return_value=httpx.Response(200, json={'stars': 10}, headers={'ETag': 'a'}))
        first = client.get('https://example.test/metrics', ttl=timedelta(seconds=-1))
        clock.now.return_value = NOW + timedelta(hours=24)
        route.mock(return_value=httpx.Response(304))
        second = client.get('https://example.test/metrics')
        assert second.fetched_at == first.fetched_at == NOW
        assert second.confirmed_at == NOW + timedelta(hours=24)
        third = client.get('https://example.test/metrics')
        assert third.cache_state == 'cached'
        assert third.confirmed_at == second.confirmed_at
        assert route.call_count == 2


def test_full_release_notes_survive_provider_and_extraction(config):
    import json
    from ai_trend_radar.http import HttpPayload
    from ai_trend_radar.providers.github import collect_watched
    body = '## Chores\n' + '- Update internal dependency bookkeeping.\n'*65 + '\n## Features\n- Add MCP tool support for AI coding agents.\n'
    class Client:
        request_count = 0
        def get(self, url, **kwargs):
            if url.endswith('/releases'):
                data = [{'id': 1, 'tag_name': 'v1.2.3', 'name': 'v1.2.3', 'body': body, 'published_at': NOW.isoformat(), 'html_url': release().canonical_url}]
            else:
                data = {'full_name': 'anthropics/claude-code', 'html_url': 'https://github.com/anthropics/claude-code'}
            return HttpPayload(json.dumps(data).encode(), 200, {}, NOW)
    config.github['watched_repositories'] = ['anthropics/claude-code']
    item = collect_watched(config, Client(), NOW).items[1]
    assert len(item.summary) == 2000
    assert item.full_text == body
    candidate = cluster_items([item], config)[0]
    attach_video_topics([candidate], config.topics)
    assert candidate.video_topic['specificity'] == 'high'
    assert 'MCP' in candidate.video_topic['primary_angle']['title']


def test_brief_reviews_defer_overflow_and_new_versions(config):
    from ai_trend_radar.state import RadarState
    from ai_trend_radar.reports import _candidate_dict
    db = Database(config.database_path); db.initialize()
    state = RadarState(db)
    def save(items, scan, at=NOW):
        state.annotate(items)
        candidates = cluster_items(items, config)
        state.bind(candidates)
        rank_candidates(candidates, config, at)
        state.save_candidates([(_candidate_dict(c, at, []), 'main') for c in candidates], scan)
        return candidates
    candidates = save([release(), release('v1.2.4')], 's1')
    first = state.brief(NOW, 1, 's1')
    assert len(first['new']) == 1
    state.acknowledge(first)
    second = state.brief(NOW, 1, 's1')
    assert len(second['new']) == 1
    assert second['new'][0]['event_id'] != first['new'][0]['event_id']
    state.acknowledge(second)
    key = candidates[0].fingerprint
    state.decide(key, 'reviewed', NOW)
    save([release(), release('v1.2.4')], 's2')
    assert not state.brief(NOW, 10, 's2')['updated']
    edited = release(); edited.summary += ' New developer SDK.'
    save([edited], 's3')
    assert state.brief(NOW, 10, 's3')['updated'][0]['event_id'] == key
    state.decide(key, 'deferred', NOW, NOW+timedelta(days=1))
    assert not state.brief(NOW, 10, 's3')['updated']
    assert not state.brief(NOW, 10, 's3')['due']
    # Restart preserves state and a new version is not suppressed.
    state = RadarState(db)
    save([release('v1.2.5')], 's4')
    assert len(state.brief(NOW, 10, 's4')['new']) == 1
    due = state.brief(NOW+timedelta(days=1), 10, 's4')
    assert due['due'][0]['event_id'] == key
    assert not due['due'][0]['current']
    state.acknowledge(due)
    assert not state.brief(NOW+timedelta(days=2), 10, 's4')['due']


def test_event_id_survives_later_exact_support(config):
    from ai_trend_radar.state import RadarState
    db = Database(config.database_path); db.initialize()
    state = RadarState(db)
    a = release()
    hn = replace(a, provider='hacker_news', external_id='123', source_family='hacker_news',
                 item_type='hacker_news_story', authority='community', metrics={})
    first = cluster_items([hn], config); state.bind(first)
    state.annotate([a, hn])
    later = cluster_items([a, hn], config); state.bind(later)
    assert later[0].fingerprint == first[0].fingerprint


def test_growth_requires_all_gates(config):
    from ai_trend_radar.state import RadarState
    for index, (hours, before, after) in enumerate([(23, 10000, 10100), (48, 100, 110), (48, 100000, 100100), (48, 10000, 9999)]):
        db = Database(config.database_path.parent / f'gate-{index}.sqlite3'); db.initialize()
        state = RadarState(db)
        item = replace(release(), external_id='repo', item_type='github_repository_snapshot',
                       published_at=NOW-timedelta(days=500), metrics={'stars': before})
        assert state.growth_events([item], config, NOW) == []
        item.observed_at = NOW + timedelta(hours=hours); item.metrics['stars'] = after
        assert state.growth_events([item], config, item.observed_at) == []


def test_native_release_recreation_is_new_but_tag_edit_is_same_event(config):
    from ai_trend_radar.state import RadarState
    db = Database(config.database_path); db.initialize()
    state = RadarState(db)
    first = release(); first.metrics['release_id'] = 1
    c1 = cluster_items([first], config); state.bind(c1)
    recreated = release(); recreated.metrics['release_id'] = 2
    state.annotate([recreated])
    c2 = cluster_items([recreated], config); state.bind(c2)
    assert c1[0].fingerprint != c2[0].fingerprint
    renamed = release('v1.2.4'); renamed.metrics['release_id'] = 2
    state.annotate([renamed])
    c3 = cluster_items([renamed], config); state.bind(c3)
    assert c2[0].fingerprint == c3[0].fingerprint


def test_project_homepage_discussion_does_not_establish_release_interest(config):
    item = release()
    hn = replace(item, provider='hacker_news', external_id='123', source_family='hacker_news',
                 item_type='hacker_news_story', authority='community',
                 canonical_url='https://github.com/anthropics/claude-code', metrics={'points': 200})
    candidate = cluster_items([item, hn], config)[0]
    attach_repository_support([candidate], [item, hn])
    rank_candidates([candidate], config, NOW)
    assert candidate.source_families == ['github']
    assert candidate.interest_band == 'early/limited'


def test_migration_preserves_legacy_data_without_trusting_metric_clocks(config):
    import sqlite3
    from ai_trend_radar.db import SCHEMA
    with sqlite3.connect(config.database_path) as cx:
        cx.executescript(SCHEMA)
        cx.execute('INSERT INTO observations VALUES (?,?,?,?)', ('github_watched', 'repo', NOW.isoformat(), '{"stars": 1}'))
    db = Database(config.database_path)
    db.initialize(); db.initialize()
    item = replace(release(), external_id='repo', metrics={'stars': 100})
    db.add_observed_growth(item)
    assert not item.metrics['observed_growth']['available']
    with db.connect() as cx:
        assert cx.execute('SELECT COUNT(*) FROM observations').fetchone()[0] == 1
        assert cx.execute('SELECT measurement_kind FROM observations').fetchone()[0] == 'legacy'
        cx.execute('PRAGMA user_version=999')
    import pytest
    with pytest.raises(RuntimeError, match='unsupported database schema'):
        db.initialize()


def test_official_content_field_and_partial_capture(config):
    from ai_trend_radar.config import OfficialFeedConfig
    from ai_trend_radar.http import HttpPayload
    from ai_trend_radar.providers.official import collect
    from ai_trend_radar.providers.common import capture_document, MAX_DOCUMENT_CHARACTERS
    config.official_feeds = [OfficialFeedConfig('test', 'https://example.test/feed', 'OpenAI')]
    content = '<p>Full SDK feature description.</p>'
    feed = f'''<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
    <guid>1</guid><title>Codex SDK release</title><link>https://example.test/release</link>
    <description>Short summary.</description><content:encoded><![CDATA[{content}]]></content:encoded>
    </item></channel></rss>'''
    class Client:
        request_count = 0
        def get(self, *args):
            return HttpPayload(feed.encode(), 200, {}, NOW, 'stale', NOW)
    item = collect(config, Client(), NOW+timedelta(days=1)).items[0]
    assert item.full_text == content
    assert item.content_complete
    assert item.cache_state == 'stale'
    assert item.measurement_time == NOW
    capture_document(item, 'x'*(MAX_DOCUMENT_CHARACTERS+1), 'markdown')
    assert not item.content_complete
    assert len(item.full_text) == MAX_DOCUMENT_CHARACTERS


def test_release_collection_survives_metadata_failure(config):
    import json
    from ai_trend_radar.http import HttpPayload
    from ai_trend_radar.providers.github import collect_watched
    config.github['watched_repositories'] = ['anthropics/claude-code']
    class Client:
        request_count = 0
        def get(self, url, **kwargs):
            if not url.endswith('/releases'):
                raise RuntimeError('metadata unavailable')
            return HttpPayload(json.dumps([{'id': 1, 'tag_name': 'v1.2.3', 'body': 'Full notes', 'published_at': NOW.isoformat()}]).encode(), 200, {}, NOW)
    result = collect_watched(config, Client(), NOW)
    assert result.status == 'partial'
    assert len(result.items) == 1
    assert result.items[0].item_type == 'github_release'


def test_cli_and_failed_brief_delivery_preserve_discoveries(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    import json
    from ai_trend_radar import pipeline
    from ai_trend_radar.cli import main
    from ai_trend_radar.config import load_config
    from ai_trend_radar.state import RadarState
    config_text = (Path(__file__).resolve().parents[1] / 'config.example.toml').read_text()
    config_path = tmp_path / 'config.toml'; config_path.write_text(config_text)
    now = datetime.now(UTC)
    unknown = SourceItem('hacker_news', '98765', 'hacker_news', 'hacker_news_story',
                        'Show HN: Unknown AI coding tool', 'A new developer tool for AI coding',
                        'https://example.test/new-tool', now, None, now, metrics={'points': 37, 'comments': 14})
    for module, method, provider in [(pipeline.official, 'collect', 'official'), (pipeline.github, 'collect_watched', 'github_watched'),
                                    (pipeline.github, 'collect_exploratory', 'github_explore'),
                                    (pipeline.huggingface, 'collect', 'huggingface')]:
        monkeypatch.setattr(module, method, lambda *args, p=provider: ProviderResult(p, 'ok', [], now))
    monkeypatch.setattr(pipeline.hackernews, 'collect', lambda *args: ProviderResult('hacker_news', 'ok', [unknown], now))
    monkeypatch.setattr(pipeline.youtube, 'validate', lambda *args, **kw: ProviderResult('youtube', 'disabled', [], now))
    original_write = pipeline._atomic_write
    def fail_brief(path, content):
        if path.name == 'latest.brief.json':
            raise OSError('disk full')
        return original_write(path, content)
    monkeypatch.setattr(pipeline, '_atomic_write', fail_brief)
    assert pipeline.run_scan(config_path, no_youtube=True) == 1
    config = load_config(config_path)
    state = RadarState(Database(config.database_path))
    pending = state.brief(now, 10)
    assert len(pending['new']) == 1
    key = pending['new'][0]['event_id']
    assert main(['brief', '--config', str(config_path)]) == 0
    assert 'Unknown AI coding tool' in capsys.readouterr().out
    assert state.brief(now, 10)['new'] == []
    assert main(['decide', key, 'reviewed', '--config', str(config_path)]) == 0
    assert main(['decide', key, 'deferred', '--until', '2030-01-01', '--config', str(config_path)]) == 1
    assert main(['decide', key, 'deferred', '--until', (now+timedelta(days=1)).isoformat(), '--config', str(config_path)]) == 0
    assert state.brief(now, 10)['due'] == []
    assert main(['decide', key, 'reopen', '--config', str(config_path)]) == 0
    assert state.brief(now, 10)['updated'][0]['event_id'] == key
    monkeypatch.setattr(pipeline, '_atomic_write', original_write)
    assert pipeline.run_scan(config_path, no_youtube=True) == 0
    saved = json.loads((config.reports_path / 'latest.brief.json').read_text())
    assert saved['updated'][0]['event_id'] == key
    full = json.loads((config.reports_path / 'latest.json').read_text())
    assert len(full['recommendations']) == 1
    assert full['recommendations'][0]['event_id'] == key


def test_delivery_does_not_erase_a_new_deferral(config):
    from ai_trend_radar.state import RadarState
    from ai_trend_radar.reports import _candidate_dict
    db = Database(config.database_path); db.initialize()
    state = RadarState(db)
    candidates = cluster_items([release()], config); state.bind(candidates)
    rank_candidates(candidates, config, NOW)
    state.save_candidates([(_candidate_dict(candidates[0], NOW, []), 'main')], 'scan')
    pending = state.brief(NOW, 10, 'scan')
    state.decide(candidates[0].fingerprint, 'deferred', NOW, NOW+timedelta(days=1))
    state.acknowledge(pending)
    assert len(state.brief(NOW+timedelta(days=1), 10, 'scan')['due']) == 1
