from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
import sqlite3

import pytest

from ai_trend_radar import developments as dev, llm_adapter, pipeline, cli
from ai_trend_radar.config import load_config, ConfigError
from ai_trend_radar.db import Database
from ai_trend_radar.developer_reports import build_developer_report
from ai_trend_radar.feedback import record_feedback, feedback_summary
from ai_trend_radar.models import Candidate, SourceItem, ProviderResult
from ai_trend_radar.page_evidence import PageUnavailable
from ai_trend_radar.reports import render_markdown
from ai_trend_radar.state import RadarState
from ai_trend_radar.topic_state import TopicState

NOW = datetime(2026, 9, 7, tzinfo=UTC)
NOTES = '## Fixes\n- Fixed the coding agent falsely reporting completed tasks when tests failed.\n\n## Features\n- Adds an API for exporting detailed agent execution traces and reviewing failed tool calls.'


def candidate(key='one', family='github', notes=NOTES):
    kind = {'github': 'github_release', 'official': 'official_announcement', 'hacker_news': 'hacker_news_story', 'huggingface': 'huggingface_model'}[family]
    source = SourceItem(family, key, family, kind, f'AI coding agent {key}', notes,
                        f'https://example.com/{key}', NOW, None, NOW,
                        authority='official' if family in {'github', 'official'} else 'community',
                        full_text=notes, content_complete=True)
    return Candidate(key, source.title, 'Example', NOW, [source], [family], discovery_priority=70)


def valid(evidence, settings=None, binary=None):
    topics = []
    for block in evidence['blocks']:
        if block['text'].startswith('#'):
            continue
        topics.append({'title': block['text'][:170], 'primary_evidence_id': block['evidence_id'],
            'change_kind': 'fix', 'what_changed': block['text'], 'who_should_care': 'Developers using coding agents.',
            'practical_difference': 'Inspect failed runs instead of assuming reported completion is correct.',
            'evidence_type': 'publisher statement',
            'evidence_quotes': [{'evidence_id': block['evidence_id'], 'quote': block['text']}],
            'caveats': ['Radar has not reproduced the publisher claim.'], 'next_step': 'Check a representative failing test.',
            'editorial': {key: {'score': score, 'reason': 'Supported by the documented workflow consequence.'} for key, score in zip(dev.WEIGHTS, (80, 90, 50))}})
    return {'status': 'valid', 'errors': [], 'tool_items': [], 'response': {'developments': topics[:3], 'abstain_reason': ''}}


@pytest.fixture
def model(monkeypatch):
    calls = []
    monkeypatch.setattr(dev.shutil, 'which', lambda _: '/test/codex')
    def run(*args):
        calls.append(args[0])
        return valid(*args)
    monkeypatch.setattr(dev.adapter, 'model_result', run)
    return calls


def test_scores_floors_and_independent_changes(config, model):
    topics, coverage = dev.assess_candidates([candidate()], config, NOW, enabled=True)
    assert len(topics) == 2 and coverage['new_calls'] == 1
    assert len({t['topic_id'] for t in topics}) == 2
    assert all(t['developer_priority']['overall'] == 77 for t in topics)
    assert all(t['disposition'] == 'main' and len(t['related_topic_ids']) == 1 for t in topics)
    assert 'video_priority' not in topics[0] and 'video_topic' not in topics[0]
    topics[0]['developer_priority'] = dev.priority({k: {'score': 100, 'reason': 'x'} for k in dev.WEIGHTS})
    assert sorted(topics, key=dev.sort_key)[0] is topics[0]


def test_fixes_survive_and_chores_are_watch_without_model(config):
    useful, _ = dev.assess_candidates([candidate()], config, NOW, enabled=False)
    assert len(useful) == 2 and any('falsely' in t['what_changed'] for t in useful)
    assert all(t['developer_priority']['overall'] is None for t in useful)
    chores, _ = dev.assess_candidates([candidate(notes='## Maintenance\n- Bump dependency versions and update internal CI configuration.')], config, NOW, enabled=False)
    assert len(chores) == 1 and chores[0]['disposition'] == 'watch'


def test_each_source_family_considered_before_top_and_budgets(config, model):
    config.llm.max_calls_per_scan = 4
    values = [candidate(f'{family}-{i}', family) for family in ('github', 'official', 'hacker_news', 'huggingface') for i in range(3)]
    topics, coverage = dev.assess_candidates(values, config, NOW, enabled=True)
    assert coverage['new_calls'] == 4
    assert all(v['assessed'] == 1 for v in coverage['by_source_family'].values())
    assert coverage['status'] == 'partial'
    assert any(t['developer_priority']['overall'] is None for t in topics)
    assert sorted(topics, key=dev.sort_key)[0]['assessment_status'] == 'assessed'


def test_document_caps_and_source_aware_urls(config, model, monkeypatch):
    config.llm.max_page_fetches = 1
    calls = []
    def page(url, _):
        calls.append(url)
        return {'text': NOTES, 'final_url': url, 'fetched_at': NOW.isoformat(), 'cache_state': 'live'}
    monkeypatch.setattr(dev, 'fetch_page', page)
    items = [candidate('repo'), candidate('hf', 'huggingface')]
    items[0].items[0].canonical_url = 'https://github.com/example/tool'
    items[1].items[0].canonical_url = 'https://huggingface.co/example/model'
    for c in items:
        c.items[0].full_text = None
    topics, coverage = dev.assess_candidates(items, config, NOW, enabled=True)
    assert coverage['page_fetches'] == 1 and len(calls) == 1
    assert calls[0] == 'https://raw.githubusercontent.com/example/tool/HEAD/README.md'
    assert dev.document_url(items[1].items[0]) == 'https://huggingface.co/example/model/raw/main/README.md'
    assert any('document budget' in e['reason'] for e in coverage['entries'])


def test_cache_identity_no_tools_and_source_anchor_stability(config, model, monkeypatch):
    c = candidate()
    first, coverage = dev.assess_candidates([c], config, NOW, enabled=True)
    second, coverage = dev.assess_candidates([c], config, NOW + timedelta(hours=1), enabled=True)
    assert coverage['cached_results'] == 1 and coverage['new_calls'] == 0
    assert [t['topic_id'] for t in first] == [t['topic_id'] for t in second]
    assert first[0]['developer_priority'] == second[0]['developer_priority']
    assert first[0]['assessed_at'] == second[0]['assessed_at']
    assert second[0]['age_hours'] == first[0]['age_hours'] + 1
    config.developer_audience = 'Developers investigating agent correctness.'
    third, coverage = dev.assess_candidates([c], config, NOW, enabled=True)
    assert coverage['new_calls'] == 1
    assert {t['topic_id'] for t in first} == {t['topic_id'] for t in third}
    assert dev.evidence_blocks('A sufficiently long source sentence.', 'id', 'url', 'claim')[0]['evidence_id'] == dev.evidence_blocks('A  sufficiently long source sentence.', 'id', 'url', 'claim')[0]['evidence_id']
    evidence = model[0]
    response = valid(evidence)['response']
    response['developments'][0]['evidence_quotes'][0]['quote'] = 'An invented quote not present in source.'
    assert llm_adapter.validate_developments(response, evidence)[1]
    response['developments'][0]['primary_evidence_id'] = 'unknown'
    with pytest.raises(ValueError):
        llm_adapter.validate_developments(response, evidence)


def test_large_html_document_does_not_collapse_distinct_changes_to_one_anchor():
    paragraphs = ['This is a documented developer consequence in paragraph number ' + str(i) + '.' for i in range(30)]
    blocks = dev.evidence_blocks('\n'.join(paragraphs), 'article', 'https://example.com', 'community report')
    assert len(blocks) == 30
    assert len({b['evidence_id'] for b in blocks}) == 30
    assert all(b['text'] in paragraphs for b in blocks)


@pytest.mark.parametrize('value', [True, -1, 101, 80.5, '80'])
def test_score_schema_rejects_invalid_values(config, model, value):
    dev.assess_candidates([candidate()], config, NOW, enabled=True)
    evidence = model[0]
    response = valid(evidence)['response']
    response['developments'][0]['editorial']['developer_impact']['score'] = value
    with pytest.raises(ValueError):
        llm_adapter.validate_developments(response, evidence)


def test_missing_dates_do_not_remove_supported_scores(config, model):
    c = candidate()
    c.items[0].published_at = None
    topics, _ = dev.assess_candidates([c], config, NOW, enabled=True)
    assert topics[0]['developer_priority']['overall'] == 77
    assert 'publication time unknown' in topics[0]['event_time_basis']


def test_failures_abstention_and_unsafe_evidence_leave_fallback(config, model, monkeypatch):
    def abstain(*_):
        return {'status': 'valid', 'response': {'developments': [], 'abstain_reason': 'Only routine maintenance.'}}
    monkeypatch.setattr(dev.adapter, 'model_result', abstain)
    topics, coverage = dev.assess_candidates([candidate()], config, NOW, enabled=True)
    assert coverage['abstentions'] == 1 and topics[0]['disposition'] == 'watch'
    assert topics[0]['developer_priority']['overall'] is None
    bad = candidate('unsafe', 'hacker_news')
    bad.items[0].full_text = None
    def unsafe(*_):
        raise PageUnavailable('Private target blocked')
    monkeypatch.setattr(dev, 'fetch_page', unsafe)
    topics, coverage = dev.assess_candidates([bad], config, NOW, enabled=True)
    assert coverage['new_calls'] == 0
    assert topics[0]['assessment_reason'] == 'Private target blocked'


def seed(config, c):
    db = Database(config.database_path)
    db.initialize()
    RadarState(db).bind([c])
    return db, TopicState(db)


def save_scan(db, key):
    db.record_scan(scan_id=key, started_at=NOW, completed_at=NOW, status='complete', config_fingerprint='test',
                   scoring_version=dev.VERSION, provider_statuses=[], report={'schema_version': '3.0'})


def test_topic_review_independence_revision_and_brief(config, model):
    c = candidate('abcdef123456')
    db, state = seed(config, c)
    topics, _ = dev.assess_candidates([c], config, NOW, enabled=True)
    state.save_topics(topics, 'first')
    save_scan(db, 'first')
    state.decide(topics[0]['topic_id'], 'reviewed', NOW)
    brief = state.brief(NOW, 10, 'first')
    assert [t['event_id'] for t in brief['new']] == [topics[1]['topic_id']]
    state.acknowledge(brief)
    assert not state.brief(NOW, 10, 'first')['new']
    changed = deepcopy(topics)
    changed[0]['title'] = 'A different generated title'
    changed[0]['developer_priority']['overall'] = 95
    state.save_topics(changed, 'second')
    save_scan(db, 'second')
    assert changed[0]['revision'] == 1
    assert not state.brief(NOW, 10, 'second')['updated']
    record_feedback(db, topics[1]['topic_id'], 'investigate', 'no', NOW, revision=1)
    assert feedback_summary(db, 'topics')['rated_unique_events'] == 1
    assert feedback_summary(db, 'legacy')['rated_unique_events'] == 0
    assert state.resolve_topic(topics[0]['topic_id'][:8]) == topics[0]['topic_id']
    with pytest.raises(ValueError, match='--event'):
        state.resolve_topic(c.fingerprint)


def test_legacy_migration_inherits_reviews_not_feedback_and_backs_up(config, model):
    c = candidate('abcdef123456')
    db, state = seed(config, c)
    payload = dev._candidate_dict(c, NOW, [])
    RadarState(db).save_candidates([(payload, 'main')], 'old')
    RadarState(db).decide(c.fingerprint, 'reviewed', NOW)
    record_feedback(db, c.fingerprint, 'investigate', 'no', NOW)
    with db.connect() as cx:
        cx.execute('PRAGMA user_version=2')
    db.initialize()
    backup = config.database_path.with_name(config.database_path.name + '.pre-v3.bak')
    assert backup.is_file()
    with sqlite3.connect(backup) as cx:
        assert cx.execute('PRAGMA user_version').fetchone()[0] == 2
    topics, _ = dev.assess_candidates([c], config, NOW, enabled=True)
    state.save_topics(topics, 'first')
    save_scan(db, 'first')
    assert all(t['review_state']['decision'] == 'reviewed' for t in topics)
    assert all(t['review_state']['inherited_state'] for t in topics)
    assert not state.brief(NOW, 10, 'first')['new']
    assert feedback_summary(db, 'topics')['rated_revisions'] == 0
    assert feedback_summary(db, 'legacy')['rated_revisions'] == 1
    c.items[0].full_text += '\n\n- Adds an API command for checking developer agent permissions before execution.'
    more, _ = dev.assess_candidates([c], config, NOW, enabled=True)
    state.save_topics(more, 'second')
    assert more[-1]['review_state']['decision'] == 'open' or any(t['review_state']['decision'] == 'open' for t in more)


def test_placeholder_replaced_without_invented_ratings(config, model):
    c = candidate()
    db, state = seed(config, c)
    pending = dev.placeholder(c, NOW, 'not assessed')
    state.save_topics([pending], 'first')
    record_feedback(db, pending['topic_id'], 'skip', 'unknown', NOW)
    state.decide(pending['topic_id'], 'deferred', NOW, NOW + timedelta(hours=1))
    topics, _ = dev.assess_candidates([c], config, NOW, enabled=True)
    state.save_topics(topics, 'second')
    save_scan(db, 'second')
    brief = state.brief(NOW + timedelta(hours=2), 10, 'second')
    assert not brief['due'] and len(brief['new']) == 2
    assert all(t['review_state']['decision'] == 'open' for t in topics)
    assert feedback_summary(db, 'topics')['rated_revisions'] == 1


def test_toggling_assessment_does_not_change_source_revision(config, model):
    c = candidate()
    db, state = seed(config, c)
    baseline, _ = dev.assess_candidates([c], config, NOW, enabled=False)
    state.save_topics(baseline, 'first')
    assessed, _ = dev.assess_candidates([c], config, NOW, enabled=True)
    state.save_topics(assessed, 'second')
    assert {t['topic_id'] for t in baseline} == {t['topic_id'] for t in assessed}
    assert all(t['revision'] == 1 for t in assessed)
    fallback, _ = dev.assess_candidates([c], config, NOW, enabled=False)
    state.save_topics(fallback, 'third')
    assert all(t['revision'] == 1 for t in fallback)


def test_migration_rolls_back_new_tables_on_failure(config, monkeypatch):
    from ai_trend_radar import topic_state
    db = Database(config.database_path)
    db.initialize()
    with db.connect() as cx:
        cx.execute('DROP TABLE development_topics')
        cx.execute('DROP TABLE legacy_topic_baselines')
        cx.execute('PRAGMA user_version=2')
    def fail(_):
        raise RuntimeError('injected migration failure')
    monkeypatch.setattr(topic_state, 'seed_baselines', fail)
    with pytest.raises(RuntimeError, match='injected'):
        db.initialize()
    with db.connect() as cx:
        assert cx.execute('PRAGMA user_version').fetchone()[0] == 2
        assert not cx.execute("SELECT name FROM sqlite_master WHERE name='development_topics'").fetchall()


def test_cached_documents_and_assessments_survive_exhausted_new_call_budget(config, model, monkeypatch):
    c = candidate('official-cache', 'official')
    c.items[0].full_text = None
    monkeypatch.setattr(dev, 'cached_page', lambda *_: {'text': NOTES, 'final_url': c.items[0].canonical_url,
        'fetched_at': NOW.isoformat(), 'cache_state': 'cached'})
    _, first = dev.assess_candidates([c], config, NOW, enabled=True)
    assert first['new_calls'] == 1 and first['page_fetches'] == 0
    config.llm.max_calls_per_scan = 1
    monkeypatch.setattr(dev, 'fetch_page', lambda *_: pytest.fail('cached evidence must not fetch'))
    _, second = dev.assess_candidates([candidate('new-release'), c], config, NOW, enabled=True)
    assert second['new_calls'] == 1 and second['cached_results'] == 1
    assert second['by_source_family']['official']['assessed'] == 1


def test_three_consecutive_failures_stop_new_calls(config, monkeypatch):
    monkeypatch.setattr(dev.shutil, 'which', lambda _: '/test/codex')
    calls = []
    def fail(*args):
        calls.append(args)
        return {'status': 'failed', 'errors': ['test failure']}
    monkeypatch.setattr(dev.adapter, 'model_result', fail)
    topics, coverage = dev.assess_candidates([candidate(str(i)) for i in range(6)], config, NOW, enabled=True)
    assert len(calls) == coverage['new_calls'] == coverage['failures'] == 3
    assert len(topics) == 12 and all(t['assessment_status'] == 'unassessed' for t in topics)


def test_below_floor_is_watch_and_unknown_dates_do_not_become_urgent(config, model, monkeypatch):
    def low(*args):
        result = valid(*args)
        for t in result['response']['developments']:
            t['editorial']['developer_impact']['score'] = 10
        return result
    monkeypatch.setattr(dev.adapter, 'model_result', low)
    topics, _ = dev.assess_candidates([candidate()], config, NOW, enabled=True)
    assert all(t['disposition'] == 'watch' and 'floors' in t['assessment_reason'] for t in topics)


def test_slack_contains_developer_questions_and_topic_ids(config, model):
    from ai_trend_radar.slack import build_payload
    c = candidate()
    db, state = seed(config, c)
    topics, _ = dev.assess_candidates([c], config, NOW, enabled=True)
    state.save_topics(topics, 'first')
    save_scan(db, 'first')
    brief = state.brief(NOW, 10, 'first')
    rendered = json.dumps(build_payload(brief))
    assert all(phrase in rendered for phrase in ['Changed:', 'For:', 'Practical impact:', '77/100', 'Impact 80', 'Relevance 90', 'Urgency 50'])
    assert all(t['topic_id'] in rendered for t in topics)
    assert 'Demo potential' not in rendered
    state.acknowledge(brief)
    state.save_topics(topics, 'second')
    save_scan(db, 'second')
    unchanged = json.dumps(build_payload(state.brief(NOW, 10, 'second')))
    assert 'No new qualifying changes' in unchanged
    assert all(t['topic_id'] not in unchanged for t in topics)


def test_plan_files_and_active_document_links():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for name in ('PLAN.md', 'plan_v2.md', 'developer-first-plan.md', 'README.md'):
        assert (root / 'docs' / name).is_file()
    assert not (root / 'PLAN.md').exists() and not (root / 'plan_v2.md').exists()
    audit = (root / 'docs/plan_v2.md').read_text()
    assert 'Shipped through v0.2.0' in audit and 'LLM' in audit and 'Hacker' in audit
    assert '(../src/' in audit and '(docs/developer-first-plan.md)' not in audit


def test_replay_uses_snapshots_without_provider_or_document_network(config, model, monkeypatch):
    c = candidate('snapshot', 'hacker_news')
    c.items[0].published_at = datetime.now(UTC)
    c.items[0].full_text = None
    snapshot = {name: ProviderResult(name, 'cached', [c.items[0]] if name == 'hacker_news' else [], NOW) for name in pipeline.DISCOVERY_PROVIDERS}
    documents = {c.items[0].canonical_url: {'text': NOTES, 'final_url': c.items[0].canonical_url,
                  'fetched_at': NOW.isoformat(), 'cache_state': 'replay'}}
    monkeypatch.setattr(dev, 'fetch_page', lambda *_: pytest.fail('replay cannot fetch a document'))
    for module, method in [(pipeline.official, 'collect'), (pipeline.github, 'collect_watched'),
        (pipeline.github, 'collect_exploratory'), (pipeline.hackernews, 'collect'), (pipeline.huggingface, 'collect')]:
        monkeypatch.setattr(module, method, lambda *_: pytest.fail('replay cannot collect providers'))
    assert pipeline.run_scan(config.source_path, config_override=config, source_snapshot=snapshot,
        document_snapshot=documents, no_youtube=True, llm=True) == 0
    report = json.loads((config.reports_path / 'latest.json').read_text())
    assert report['assessment']['new_calls'] == 1
    assert report['assessment']['page_fetches'] == 0
    assert 'replay' in report['assessment']['warnings'][-1]
    # A missing captured page is an explicit gap, never an invitation to fetch.
    more, coverage = dev.assess_candidates([c], config, NOW, enabled=True, document_snapshot={})
    assert coverage['new_calls'] == 0 and 'not captured in replay' in more[0]['assessment_reason']


def test_report_contract_order_numbering_and_optional_appendix(config, model):
    topics, coverage = dev.assess_candidates([candidate()], config, NOW, enabled=True)
    topics += [dev.placeholder(candidate('pending', 'hacker_news'), NOW, 'budget')]
    report = build_developer_report(scan_id='test', started=NOW, completed=NOW, status='partial', config=config,
        providers=[], topics=topics, coverage=coverage, top=3, youtube_appendix=[])
    text = render_markdown(report)
    assert report['schema_version'] == '3.0'
    assert report['recommendations'][-1]['assessment_status'] == 'unassessed'
    assert '### 1.' in text and '### 3.' in text
    assert 'What changed:' in text and 'Who should care:' in text and 'Practical difference:' in text
    assert 'Demo potential' not in text and 'LLM-discovered updates' not in text and 'YouTube' not in text
    report['youtube_appendix'] = [{'title': 'Optional search', 'evidence': {'manual_search_urls': ['https://youtube.com/']}}]
    assert render_markdown(report).index('Optional YouTube appendix') > render_markdown(report).index('## Watch')
    report['provider_status'] = [
        {'provider': 'first', 'status': 'failed', 'item_count': 0, 'error': 'Unavailable'},
        {'provider': 'second', 'status': 'ok', 'item_count': 2},
    ]
    text = render_markdown(report)
    assert '| first | failed | 0 |\n| second | ok | 2 |' in text
    assert text.index('first: Unavailable') > text.index('| second | ok | 2 |')


def test_developer_config_legacy_precedence_and_youtube_defaults(tmp_path):
    path = tmp_path / 'config.toml'
    path.write_text('[llm]\naudience = "Legacy audience"\n[developer]\naudience = "New audience"\n')
    config = load_config(path)
    assert config.developer_audience == 'New audience' and not config.youtube['enabled']
    assert not config.llm.enabled
    path.write_text('[llm]\naudience = "Legacy audience"\n')
    with pytest.warns(FutureWarning):
        assert load_config(path).developer_audience == 'Legacy audience'
    path.write_text('[developer]\naudience = ""\n')
    with pytest.raises(ConfigError):
        load_config(path)
    assert cli.build_parser().parse_args(['scan', '--youtube']).youtube
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(['scan', '--youtube', '--no-youtube'])


def test_pipeline_no_youtube_and_same_topics_in_brief(config, model, monkeypatch):
    c = candidate()
    # Use a live relative timestamp so this fixture always stays in the lookback.
    c.items[0].published_at = datetime.now(UTC)
    monkeypatch.setattr(pipeline, 'load_config', lambda _: config)
    for module, method, provider in [(pipeline.official, 'collect', 'official'), (pipeline.github, 'collect_watched', 'github_watched'),
        (pipeline.github, 'collect_exploratory', 'github_explore'), (pipeline.hackernews, 'collect', 'hacker_news'), (pipeline.huggingface, 'collect', 'huggingface')]:
        monkeypatch.setattr(module, method, lambda *_, p=provider: ProviderResult(p, 'ok', [c.items[0]] if p == 'github_watched' else [], NOW))
    monkeypatch.setattr(pipeline.youtube, 'validate', lambda *_a, **_k: pytest.fail('YouTube must be opt-in'))
    assert pipeline.run_scan(config.source_path, llm=True) == 0
    report = json.loads((config.reports_path / 'latest.json').read_text())
    brief = json.loads((config.reports_path / 'latest.brief.json').read_text())
    assert len(report['recommendations']) == 2
    assert {t['topic_id'] for t in report['recommendations']} == {t['event_id'] for t in brief['new']}
    assert pipeline.run_scan(config.source_path, llm=True) == 0
    assert not json.loads((config.reports_path / 'latest.brief.json').read_text())['new']
