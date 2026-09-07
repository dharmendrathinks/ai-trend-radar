from datetime import UTC, datetime, timedelta
import json

import pytest

from ai_trend_radar.db import Database
from ai_trend_radar.discovery import RepositoryDiscovery
from ai_trend_radar.feedback import record_feedback, feedback_summary
from ai_trend_radar.http import HttpPayload
from ai_trend_radar.models import isoformat
from ai_trend_radar.providers.github import collect_exploratory
from ai_trend_radar.ranking import eligible_items
from ai_trend_radar.state import RadarState


NOW = datetime(2026, 9, 6, tzinfo=UTC)


def repository(name='example/agent', stars=100):
    return {'full_name': name, 'description': 'AI coding agent SDK',
            'html_url': f'https://github.com/{name}', 'created_at': '2023-01-01T00:00:00Z',
            'pushed_at': isoformat(NOW), 'stargazers_count': stars, 'topics': ['coding-agent']}


class Client:
    def __init__(self, database, now, search=(), repos=None, cache='live', incomplete=False):
        self.database, self.now, self.search = database, now, search
        self.repos, self.cache, self.incomplete = repos or {}, cache, incomplete
        self.calls = []
        self.request_count = 0

    def get(self, url, params=None):
        self.calls.append((url, params))
        self.request_count += 1
        if url.endswith('/search/repositories'):
            data = {'items': self.search, 'incomplete_results': self.incomplete}
        else:
            data = self.repos[url.split('/repos/')[1]]
            if isinstance(data, Exception):
                raise data
        return HttpPayload(json.dumps(data).encode(), 200, {}, self.now, self.cache)


@pytest.fixture
def discovery(config):
    config.github['watched_repositories'] = []
    config.github['exploration_queries'] = []
    config.github['established_queries'] = ['topic:coding-agent pushed:>{since}']
    database = Database(config.database_path)
    database.initialize()
    return database, config


def test_older_repository_requires_measured_growth_and_survives_leaving_search(discovery):
    db, config = discovery
    state = RadarState(db)
    first = collect_exploratory(config, Client(db, NOW, [repository()]), NOW)
    assert first.items[0].published_at.year == 2023
    assert eligible_items(first.items, config, NOW) == []
    assert state.growth_events(first.items, config, NOW) == []
    db.record_provider_result(first)

    later = NOW + timedelta(hours=48)
    client = Client(db, later, repos={'example/agent': repository(stars=175)})
    result = collect_exploratory(config, client, later)
    assert any('/repos/example/agent' in url for url, _ in client.calls)
    events = RadarState(db).growth_events(result.items, config, later)
    assert len(events) == 1
    assert events[0].metrics['observed_growth']['metrics']['stars']['delta'] == 75
    assert events[0].published_at == later
    assert eligible_items(events, config, later)
    unchanged = collect_exploratory(config, Client(db, later + timedelta(days=1), repos={'example/agent': repository(stars=175)}), later + timedelta(days=1))
    assert [i.external_id for i in state.growth_events(unchanged.items, config, later + timedelta(days=1))] == [events[0].external_id]


def test_stale_search_cannot_admit_and_cached_followup_cannot_measure_growth(discovery):
    db, config = discovery
    result = collect_exploratory(config, Client(db, NOW, [repository()], cache='stale'), NOW)
    assert not result.items and RepositoryDiscovery(db).count() == 0
    first = collect_exploratory(config, Client(db, NOW, [repository()]), NOW)
    state = RadarState(db)
    state.growth_events(first.items, config, NOW)
    later = NOW + timedelta(days=2)
    stale = collect_exploratory(config, Client(db, later, repos={'example/agent': repository(stars=500)}, cache='cached'), later)
    assert state.growth_events(stale.items, config, later) == []


def test_tracking_capacity_failure_rotation_expiry_and_readmission(discovery):
    db, config = discovery
    config.github.update(established_tracking_limit=2, established_followup_per_scan=1, established_tracking_days=3)
    result = collect_exploratory(config, Client(db, NOW, [repository('a/agent'), repository('b/agent'), repository('c/agent')]), NOW)
    assert len(result.items) == 2
    RadarState(db).growth_events(result.items, config, NOW)
    failure = collect_exploratory(config, Client(db, NOW + timedelta(days=1), repos={'a/agent': RuntimeError('offline')}), NOW + timedelta(days=1))
    assert 'offline' in failure.error
    client = Client(db, NOW + timedelta(days=2), repos={'b/agent': repository('b/agent')})
    collect_exploratory(config, client, NOW + timedelta(days=2))
    assert client.calls[0][0].endswith('/repos/b/agent')
    # Expiry removes admission and resets its growth baseline on subsequent re-entry.
    later = NOW + timedelta(days=4)
    new = collect_exploratory(config, Client(db, later, [repository('a/agent', 500)]), later)
    assert RadarState(db).growth_events(new.items, config, later) == []


def test_new_lane_filters_noise_preserves_new_repo_budget_and_reports_incomplete(discovery):
    db, config = discovery
    config.github['exploration_queries'] = ['created:>{since} topic:coding-agent']
    config.github['watched_repositories'] = ['watched/agent']
    pool = [repository('watched/agent'), dict(repository('private/agent'), private=True),
            dict(repository('archived/agent'), archived=True), dict(repository('fork/agent'), fork=True),
            dict(repository('irrelevant/project'), description='Recipes', topics=[])]
    client = Client(db, NOW, pool, incomplete=True)
    result = collect_exploratory(config, client, NOW)
    assert len(result.items) == 5  # Existing exploration behavior is preserved.
    assert RepositoryDiscovery(db).count() == 0
    assert result.status == 'partial' and 'incomplete' in result.error
    assert client.calls[0][1]['sort'] == 'stars'
    assert client.calls[1][1]['sort'] == 'updated'
    assert client.calls[0][1]['per_page'] == config.github['exploration_per_query']
    assert client.calls[1][1]['per_page'] == config.github['established_per_query']


def test_overlapping_searches_do_not_hide_established_observation(discovery):
    db, config = discovery
    config.github['exploration_queries'] = ['topic:coding-agent']
    result = collect_exploratory(config, Client(db, NOW, [repository()]), NOW)
    assert len(result.items) == 1
    assert result.items[0].item_type == 'github_repository_snapshot'
    assert RepositoryDiscovery(db).count() == 1


def seed_event(db, key='abcd1234', revision=1):
    payload = {'observed_signals': [{'source_family': 'github', 'evidence_role': 'event',
                                     'metrics': {'discovery_origin': 'established_repository_search'}},
                                    {'source_family': 'hacker_news', 'evidence_role': 'project_context'}]}
    with db.connect() as cx:
        cx.execute('INSERT INTO radar_events(event_id,revision,payload_json,decision) VALUES (?,?,?,?)',
                   (key, revision, json.dumps(payload), 'deferred'))


def test_feedback_preserves_decisions_revisions_and_latest_judgment(discovery):
    db, _ = discovery
    seed_event(db)
    assert record_feedback(db, 'abcd', 'investigate', 'no', NOW, revision=1) == 'abcd1234'
    record_feedback(db, 'abcd', 'brief', 'yes', NOW + timedelta(hours=1))
    with db.connect() as cx:
        assert cx.execute('SELECT decision FROM radar_events').fetchone()[0] == 'deferred'
        assert cx.execute('SELECT COUNT(*) FROM research_feedback').fetchone()[0] == 2
    summary = feedback_summary(db)
    assert summary['rated_revisions'] == 1 and summary['feedback_history_count'] == 2
    assert summary['verdicts'] == {'investigate': 0, 'brief': 1, 'skip': 0}
    assert summary['useful_previously_unknown_events'] == 0
    assert list(summary['by_source_family']) == ['github']
    assert list(summary['by_discovery_origin']) == ['established_repository_search']
    with db.connect() as cx:
        cx.execute('UPDATE radar_events SET revision=2')
    with pytest.raises(ValueError, match='changed'):
        record_feedback(db, 'abcd', 'skip', 'no', NOW, revision=1)
    record_feedback(db, 'abcd', 'investigate', 'no', NOW, revision=2)
    assert feedback_summary(db)['rated_revisions'] == 2
    assert feedback_summary(db)['useful_previously_unknown_events'] == 1


def test_presentation_denominator_is_explicit_idempotent_and_preserves_unknown(discovery):
    db, _ = discovery
    seed_event(db)
    seed_event(db, 'bbbb1234')
    brief = {'new': [{'event_id': 'abcd1234', 'revision': 1}, {'event_id': 'bbbb1234', 'revision': 1}],
             'updated': [], 'due': [], 'generated_at': isoformat(NOW)}
    state = RadarState(db)
    assert feedback_summary(db)['presented_revisions'] == 0
    state.acknowledge(brief)
    state.acknowledge(brief)
    record_feedback(db, 'abcd', 'investigate', 'unknown', NOW)
    summary = feedback_summary(db)
    assert summary['presented_revisions'] == 2 and summary['rated_presented_revisions'] == 1
    assert summary['unrated_presented_revisions'] == 1
    assert summary['already_known']['unknown'] == 1
    assert summary['useful_previously_unknown_events'] == 0
    with pytest.raises(ValueError, match='unknown or ambiguous'):
        record_feedback(db, 'ffff', 'skip', 'no', NOW)
    seed_event(db, 'abcd5678')
    with pytest.raises(ValueError, match='unknown or ambiguous'):
        record_feedback(db, 'abcd', 'skip', 'no', NOW)


def test_additive_schema_upgrade_preserves_events_and_does_not_invent_presentations(config):
    db = Database(config.database_path)
    db.initialize()
    seed_event(db)
    with db.connect() as cx:
        cx.execute('DROP TABLE research_feedback')
        cx.execute('DROP TABLE brief_presentations')
        cx.execute('DROP TABLE discovery_repositories')
        cx.execute('PRAGMA user_version=1')
    db.initialize()
    db.initialize()
    with db.connect() as cx:
        assert cx.execute('PRAGMA user_version').fetchone()[0] == 2
        assert cx.execute('SELECT COUNT(*) FROM radar_events').fetchone()[0] == 1
        assert cx.execute('SELECT COUNT(*) FROM brief_presentations').fetchone()[0] == 0


def test_feedback_cli_is_offline_and_summary_explains_limits(discovery, monkeypatch, capsys):
    from ai_trend_radar import cli, config as config_module
    db, config = discovery
    seed_event(db)
    monkeypatch.setattr(config_module, 'load_config', lambda _: config)
    assert cli.main(['feedback', 'abcd', 'investigate', '--known', 'no', '--revision', '1']) == 0
    assert cli.main(['feedback-summary', '--json']) == 0
    output = capsys.readouterr().out
    assert '"useful_previously_unknown_events": 1' in output
    assert 'not precision' in output


def test_established_config_limits_and_legacy_opt_out(tmp_path):
    from ai_trend_radar.config import load_config, ConfigError
    path = tmp_path / 'config.toml'
    path.write_text('[github]\nestablished_followup_per_scan=21\n')
    with pytest.raises(ConfigError, match='must be <= 20'):
        load_config(path)
    path.write_text('[github]\nestablished_queries="not a list"\n')
    with pytest.raises(ConfigError, match='list'):
        load_config(path)
    path.write_text('[github]\n')
    assert load_config(path).github.get('established_queries', []) == []


def test_two_scans_surface_older_growth_and_measure_its_feedback(discovery, monkeypatch):
    from ai_trend_radar import pipeline
    from ai_trend_radar.models import ProviderResult
    db, config = discovery
    at = NOW

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at

    monkeypatch.setattr(pipeline, 'datetime', Clock)
    monkeypatch.setattr(pipeline, 'load_config', lambda _: config)
    monkeypatch.setattr(pipeline.github, 'build_client', lambda *args: Client(
        db, at, [repository()] if at == NOW else [], repos={'example/agent': repository(stars=175)}))
    for module, name, provider in [(pipeline.official, 'collect', 'official'),
                                    (pipeline.github, 'collect_watched', 'github_watched'),
                                    (pipeline.hackernews, 'collect', 'hacker_news'),
                                    (pipeline.huggingface, 'collect', 'huggingface')]:
        monkeypatch.setattr(module, name, lambda *args, p=provider: ProviderResult(p, 'ok', [], at))
    monkeypatch.setattr(pipeline.youtube, 'validate', lambda *args, **kwargs: ProviderResult('youtube', 'disabled', [], at))
    assert pipeline.run_scan(config.source_path, no_youtube=True) == 0
    first = json.loads((config.reports_path / 'latest.brief.json').read_text())
    assert first['new'] == []
    at = NOW + timedelta(days=2)
    assert pipeline.run_scan(config.source_path, no_youtube=True) == 0
    brief = json.loads((config.reports_path / 'latest.brief.json').read_text())
    assert len(brief['new']) == 1
    event = brief['new'][0]
    assert 'observed star growth' in event['candidate']['title']
    record_feedback(db, event['event_id'], 'investigate', 'no', at, revision=event['revision'])
    summary = feedback_summary(db)
    assert summary['presented_revisions'] == summary['rated_presented_revisions'] == 1
    assert summary['by_discovery_origin']['established_repository_search']['useful_previously_unknown_revisions'] == 1
    assert 'Established repository follow-up: 1/20 slots' in (config.reports_path / 'latest.md').read_text()
