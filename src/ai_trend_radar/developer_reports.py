"""Report schema 3: one developer-focused list and an optional YouTube appendix."""
from ai_trend_radar.developments import LABELS, VERSION, WEIGHTS, sort_key
from ai_trend_radar.models import isoformat


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def topic_markdown(topic, number):
    p = topic['developer_priority']
    overall = p['overall'] if p['overall'] is not None else 'N/A'
    lines = [f"### {str(number) + '. ' if number is not None else ''}{topic['title']}", '',
             f"Topic: `{topic['topic_id']}` · Overall: **{overall}{'/100' if overall != 'N/A' else ''}** · {topic['assessment_status']}", '',
             f"**What changed:** {topic['what_changed']}", '',
             f"**Who should care:** {topic['who_should_care']}", '',
             f"**Practical difference:** {topic['practical_difference']}", '',
             f"Source time: {topic['event_time']} ({topic['event_time_basis']}); age {topic['age_hours']:.1f}h. Assessment: {topic.get('assessed_at') or 'not available'}.", '',
             f"Evidence: {topic['evidence_type']}. Radar has not independently reproduced these claims.", '']
    for key, label in LABELS.items():
        category = p['categories'][key]
        score = category['score'] if category['score'] is not None else 'N/A'
        lines.append(f"- {label}: {score} — {category['reason']}")
    lines.extend(['', topic['assessment_reason'], ''])
    for evidence in topic['evidence_quotes']:
        lines.extend(['> ' + evidence['quote'].replace('\n', '\n> '), '', f"[Source evidence]({evidence['source_url']})", ''])
    if topic['caveats']:
        lines.extend(['Caveats:', '', *('- ' + c for c in topic['caveats']), ''])
    if topic['next_step']:
        lines.extend([f"Worth checking: {topic['next_step']}", ''])
    state = topic.get('review_state', {})
    if state:
        lines.extend([f"Review state: {state.get('decision', 'open')}", ''])
    if state.get('inherited_state'):
        lines.extend([f"Inherited history: {state['inherited_state']}", ''])
    if topic['related_topic_ids']:
        lines.extend(['Related developments: ' + ', '.join(f'`{key}`' for key in topic['related_topic_ids']), ''])
    lines.extend(['Sources:', '', *('- ' + url for url in topic['source_links']), '',
                  f"Review: `ai-trend-radar decide {topic['topic_id']} reviewed`", '',
                  f"Feedback: `ai-trend-radar feedback {topic['topic_id']} investigate --known no --revision {topic.get('revision', 1)}`", '',
                  f"Defer: `ai-trend-radar decide {topic['topic_id']} deferred --until <ISO timestamp with timezone>`", ''])
    return lines


def build_developer_report(*, scan_id, started, completed, status, config, providers, topics, coverage, top, youtube_appendix):
    ordered = sorted(topics, key=sort_key)
    main = [t for t in ordered if t['disposition'] == 'main']
    for index, topic in enumerate(main, 1):
        topic['rank'] = index
    return {'schema_version': '3.0', 'scoring_version': VERSION, 'scan_id': scan_id,
            'started_at': isoformat(started), 'generated_at': isoformat(completed), 'status': status,
            'configuration_fingerprint': config.fingerprint,
            'purpose': 'What changed, which developers should care, and what difference could it make to their work?',
            'audience': config.developer_audience, 'ranking': {'version': VERSION, 'weights': WEIGHTS,
            'order': 'Assessed: overall, impact, relevance, urgency, event time, ID. Unassessed last in Discovery Priority order.',
            'floors': {'overall': 50, 'developer_impact': 25, 'developer_relevance': 50}},
            'provider_status': [p.status_dict() for p in providers], 'assessment': coverage,
            'recommendations': main[:top], 'pending_count': max(0, len(main) - top),
            'watch': [t for t in ordered if t['disposition'] != 'main'], 'youtube_appendix': youtube_appendix}


def render_developer_report(report):
    lines = ['# AI Trend Radar', '', '> ' + report['purpose'], '',
             f"Generated: {report['generated_at']} · Scan: `{report['scan_id']}` · Status: {report['status']}", '',
             '## Developer updates', '', f"Audience: {report['audience']}", '',
             'Impact, relevance and urgency are editorial judgments out of 100, not measured outcomes. '
             'Overall = 50% impact + 30% relevance + 20% urgency. Urgency describes source circumstances at assessment time, '
             'not recency or a live deadline countdown. Evidence type and age are separate.', '',
             'Assessment-first ordering: scored qualifying developments precede unassessed discoveries. '
             'N/A means not assessed, not low usefulness. Without an LLM, ordering is deterministic Discovery Priority only.', '',
             '| # | Topic | Impact /100 | Relevance /100 | Urgency /100 | Overall /100 | Assessment |',
             '|---:|---|---:|---:|---:|---:|---|']
    for number, t in enumerate(report['recommendations'], 1):
        p = t['developer_priority']
        scores = [p['categories'][k]['score'] for k in LABELS] + [p['overall']]
        lines.append(f"| {number} | {cell(t['title'])} | " + ' | '.join(str(s) if s is not None else 'N/A' for s in scores) + f" | {t['assessment_status']} |")
    lines.append('')
    if not report['recommendations']:
        lines.extend(['No qualifying developments in this scan. This is a valid result.', ''])
    for number, topic in enumerate(report['recommendations'], 1):
        lines.extend(topic_markdown(topic, number))
    lines.extend(['## Coverage and limitations', '', f"{report['pending_count']} qualifying developments beyond the full-report limit remain available to the changes inbox.", '',
                  '| Provider | Status | Items |', '|---|---|---:|'])
    for p in report['provider_status']:
        lines.append(f"| {p['provider']} | {p['status']} | {p['item_count']} |")
    lines.append('')
    for p in report['provider_status']:
        if p.get('error'):
            lines.extend(['', f"{p['provider']}: {p['error']}", ''])
        tracking = p.get('details', {}).get('established_tracking')
        if tracking:
            lines.extend(['', f"Established repository follow-up: {tracking['active']}/{tracking['limit']} slots; {tracking['sampled']} samples returned. Activity starts observation, not a trend claim.", ''])
    a = report['assessment']
    lines.extend(['', f"Assessment: {a['status']} · considered {a['considered']}/{a['candidate_events']} events · {a['new_calls']} new model calls · {a['cached_results']} cached · {a['page_fetches']} document reads · {a['failures']} failures · {a['abstentions']} abstentions · {a['skipped']} skipped.", '', a['selection'], ''])
    for family, counts in sorted(a['by_source_family'].items()):
        lines.append(f"- {family}: {counts['candidates']} candidates; {counts['considered']} considered; {counts['assessed']} assessed.")
    lines.append('')
    for e in a['entries']:
        if e['reason']:
            lines.append(f"- [{e['title']}]({e['source_url']}): {e['status']} — {e['reason']}")
    lines.extend(['', *a['warnings'], '', '## Watch', ''])
    for index, topic in enumerate(report['watch'], 1):
        lines.extend(topic_markdown(topic, index))
    if not report['watch']:
        lines.extend(['None.', ''])
    if report['youtube_appendix']:
        lines.extend(['## Optional YouTube appendix', '', 'Manual inspection only; no role in developer selection or scoring.', ''])
        for entry in report['youtube_appendix']:
            lines.extend([f"### {entry['title']}", ''])
            data = entry['evidence']
            lines.extend('- ' + url for url in data.get('manual_search_urls', []))
            for video in data.get('videos', []):
                lines.append(f"- {video.get('title', '')}: {video.get('url', '')}")
            lines.append('')
    return '\n'.join(lines)
