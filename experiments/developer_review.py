"""One opt-in live scan in fresh isolated state; never deliver Slack or YouTube."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import shutil
import logging
import json
import sqlite3
from datetime import datetime

from dotenv import load_dotenv

from ai_trend_radar.config import DEVELOPER_AUDIENCE, load_config
from ai_trend_radar.pipeline import run_scan
from ai_trend_radar.pipeline import DISCOVERY_PROVIDERS
from ai_trend_radar.models import ProviderResult, SourceItem
from ai_trend_radar.llm_adapter import digest


def replay_inputs(directory, output, retry_failed=False):
    report = json.loads((directory / 'latest.json').read_text())
    captured_at = datetime.fromisoformat(report['started_at'].replace('Z', '+00:00'))
    with sqlite3.connect((directory / 'radar.sqlite3').as_uri() + '?mode=ro', uri=True) as cx:
        items = [SourceItem.from_dict(json.loads(r[0])) for r in cx.execute('SELECT payload_json FROM source_items')]
    results = {name: ProviderResult(name, 'cached', [i for i in items if i.provider == name], captured_at,
               details={'replay_of': report['scan_id'], 'not_rechecked': True}) for name in DISCOVERY_PROVIDERS}
    documents = {}
    for path in (directory / 'radar.pages').glob('*.json'):
        page = json.loads(path.read_text())
        if digest(page['text']) != page['text_sha256']:
            raise ValueError('captured document checksum mismatch')
        # This is a historical evidence artifact, not a fresh HTTP cache response.
        documents[page['requested_url']] = {**page, 'cache_state': 'replay'}
    destination = output / 'radar.llm'
    destination.mkdir()
    for path in (directory / 'radar.llm').glob('developer-*.json'):
        record = json.loads(path.read_text())
        if retry_failed and record['result']['status'] == 'failed':
            continue
        shutil.copy2(path, destination / path.name)
    return results, documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=Path('config.toml'))
    parser.add_argument('--output', type=Path, required=True, help='new, nonexistent review directory')
    parser.add_argument('--replay-from', type=Path, help='prior isolated review directory; no source-network requests')
    parser.add_argument('--max-calls', type=int, help='new model-call cap; replay defaults to zero')
    parser.add_argument('--retry-failed', action='store_true', help='explicitly retry failed assessments during replay, within the cap')
    args = parser.parse_args()
    load_dotenv(Path.cwd() / '.env')
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)
    config = load_config(args.config)
    max_calls = args.max_calls if args.max_calls is not None else (0 if args.replay_from else config.llm.max_calls_per_scan)
    if not 0 <= max_calls <= config.llm.max_calls_per_scan:
        parser.error('max-calls must be between zero and the configured allowance')
    if args.retry_failed and not args.replay_from:
        parser.error('retry-failed requires replay-from')
    output = args.output.resolve()
    if output.exists():
        parser.error('output must not exist; preserve previous reviews')
    if output == config.reports_path or output == config.database_path.parent:
        parser.error('output must not be a production state directory')
    output.mkdir(parents=True)
    # Snapshot the last report, not the live database or authentication/config files.
    for name in ('latest.md', 'latest.json'):
        previous = config.reports_path / name
        if previous.is_file():
            shutil.copy2(previous, output / ('baseline-' + name))
    raw = deepcopy(config.raw)
    raw['paths'] = {'database': str(output / 'radar.sqlite3'), 'reports': str(output)}
    raw['developer'] = {'audience': DEVELOPER_AUDIENCE}
    raw.setdefault('youtube', {})['enabled'] = False
    raw.setdefault('llm', {})['enabled'] = True
    raw['llm']['max_calls_per_scan'] = max_calls
    isolated = replace(config, database_path=output / 'radar.sqlite3', reports_path=output,
                       youtube={**config.youtube, 'enabled': False}, raw=raw,
                       llm=replace(config.llm, max_calls_per_scan=max_calls), developer_audience=DEVELOPER_AUDIENCE)
    snapshot, documents = replay_inputs(args.replay_from.resolve(), output, args.retry_failed) if args.replay_from else (None, None)
    print(f'Isolated {"replay" if args.replay_from else "live"} review: {output}; at most {max_calls} new model calls. No Slack or YouTube.', flush=True)
    return run_scan(args.config, no_youtube=True, slack=False, llm=True, config_override=isolated,
                    source_snapshot=snapshot, document_snapshot=documents)


if __name__ == '__main__':
    raise SystemExit(main())
