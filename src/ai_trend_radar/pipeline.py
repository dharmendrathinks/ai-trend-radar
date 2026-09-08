from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from uuid import uuid4
import logging
import json
import os
import sqlite3
import sys

from ai_trend_radar.config import ConfigError, load_config
from ai_trend_radar.db import Database
from ai_trend_radar.http import CachedHttpClient
from ai_trend_radar.models import ProviderResult
from ai_trend_radar.providers import github, hackernews, huggingface, official, youtube
from ai_trend_radar.ranking import attach_repository_support, eligible_items, rank_candidates
from ai_trend_radar.reports import write_reports, _atomic_write
from ai_trend_radar.state import RadarState, render_brief
from ai_trend_radar.resolution import cluster_items
from ai_trend_radar.utils import compact_error
from ai_trend_radar.slack import SlackDelivery, validate_webhook


LOGGER = logging.getLogger(__name__)
DISCOVERY_PROVIDERS = ["official", "github_watched", "github_explore", "hacker_news", "huggingface"]


def _failed(name: str, now: datetime, exc: BaseException) -> ProviderResult:
    return ProviderResult(name, "failed", [], now, error=compact_error(exc))


def run_scan(config_path: Path, *, top: int | None = None, no_youtube: bool = False, slack: bool = False, llm: bool | None = None, youtube_enabled: bool | None = None, config_override=None, source_snapshot=None, document_snapshot=None) -> int:
    started = datetime.now(UTC)
    scan_id = uuid4().hex[:10]
    try:
        config = config_override or load_config(config_path)
        if youtube_enabled is not None:
            config.youtube["enabled"] = youtube_enabled
        delivery = SlackDelivery(config.database_path, validate_webhook(os.getenv("SLACK_WEBHOOK_URL"))) if slack else None
        if top is not None and top <= 0:
            raise ConfigError("--top must be positive")
        result_count = top or config.top_results
        database = Database(config.database_path)
        database.initialize()

        tasks: dict[str, Callable[[], ProviderResult]] = {
            "official": lambda: official.collect(config, CachedHttpClient(database, config.http), started),
            "github_watched": lambda: github.collect_watched(config, github.build_client(config, database), started),
            "github_explore": lambda: github.collect_exploratory(config, github.build_client(config, database), started),
            "hacker_news": lambda: hackernews.collect(config, CachedHttpClient(database, config.http), started),
            "huggingface": lambda: huggingface.collect(config, started),
        }
        collected: dict[str, ProviderResult] = {}
        if source_snapshot is not None:
            collected = dict(source_snapshot)
        else:
            with ThreadPoolExecutor(max_workers=min(config.max_workers, len(tasks))) as executor:
                futures = {executor.submit(task): name for name, task in tasks.items()}
                for future in as_completed(futures):
                    name = futures[future]
                    try:
                        collected[name] = future.result()
                    except Exception as exc:
                        collected[name] = _failed(name, started, exc)
                    result = collected[name]
                    LOGGER.info("%s: %s (%d items)", name, result.status, len(result.items))

        provider_results = [collected[name] for name in DISCOVERY_PROVIDERS]
        usable = [result for result in provider_results if result.status in {"ok", "partial", "cached", "stale"}]
        if not usable:
            details = "; ".join(f"{result.provider}: {result.error}" for result in provider_results)
            raise RuntimeError(f"all discovery providers are unavailable: {details}")

        all_items = []
        for result in provider_results:
            for item in result.items:
                database.add_observed_growth(item)
            database.record_provider_result(result)
            all_items.extend(result.items)

        state = RadarState(database)
        growth = state.growth_events(all_items, config, started)
        state.annotate([*all_items, *growth])
        eligible = eligible_items([*all_items, *growth], config, started)
        candidates = cluster_items(eligible, config)
        attach_repository_support(candidates, all_items)
        state.bind(candidates)
        candidates = rank_candidates(candidates, config, started)
        from ai_trend_radar.developments import assess_candidates
        from ai_trend_radar.developer_reports import build_developer_report
        from ai_trend_radar.topic_state import TopicState

        topics, coverage = assess_candidates(candidates, config, started,
            enabled=config.llm.enabled if llm is None else llm, document_snapshot=document_snapshot)
        if source_snapshot is not None:
            coverage['warnings'].append('Captured-evidence replay; providers and source documents were not re-fetched.')
        topic_state = TopicState(database)
        topic_state.save_topics(topics, scan_id)
        selected_topics = [t for t in topics if t["disposition"] == "main"][:result_count]
        parent_ids = {p for t in selected_topics for p in t["parent_event_ids"]}
        selected = [c for c in candidates if c.fingerprint in parent_ids]
        youtube_appendix = []
        youtube_requested = bool(config.youtube.get("enabled", False)) and not no_youtube
        if youtube_requested:
            youtube_key = os.getenv("YOUTUBE_API_KEY")
            youtube_client = CachedHttpClient(database, config.http, secrets=[youtube_key] if youtube_key else [])
            youtube_result = youtube.validate(selected, config, youtube_client, started, disabled=False)
            provider_results.append(youtube_result)
            youtube_appendix = [{"parent_event_id": c.fingerprint, "title": c.title, "evidence": c.youtube} for c in selected]
        completed = datetime.now(UTC)
        partial = any(p.status in {"failed", "partial", "stale"} for p in provider_results) or coverage["status"] == "partial"
        status = "partial" if partial else "complete"
        report = build_developer_report(scan_id=scan_id, started=started, completed=completed,
            status=status, config=config, providers=provider_results, topics=topics, coverage=coverage,
            top=result_count, youtube_appendix=youtube_appendix)
        markdown_path, json_path = write_reports(report, config.reports_path)
        database.record_scan(scan_id=scan_id, started_at=started, completed_at=completed, status=status,
            config_fingerprint=config.fingerprint, scoring_version=report["scoring_version"],
            provider_statuses=[p.status_dict() for p in provider_results], report=report)
        brief = topic_state.brief(completed, result_count, scan_id)
        brief["provider_status"] = report["provider_status"]
        brief["assessment_status"] = coverage["status"]
        brief["status"] = status
        _atomic_write(config.reports_path / "latest.brief.md", render_brief(brief))
        _atomic_write(config.reports_path / "latest.brief.json", json.dumps(brief, indent=2, ensure_ascii=False) + "\n")
        if delivery:
            delivery.enqueue(brief)
        topic_state.acknowledge(brief)
        print(f"Changes: {config.reports_path / 'latest.brief.md'}")
        print(f"Scan {scan_id}: {status}; {len(selected_topics)} developer updates; {len(report['watch'])} watch")
        print(f"Assessment: {coverage['new_calls']} new calls; {coverage['cached_results']} cached; {coverage['skipped']} skipped")
        print(f"Markdown: {markdown_path}")
        print(f"JSON: {json_path}")
        if delivery:
            try:
                sent, pending = delivery.send_pending()
                print(f"Slack: {sent} sent; {pending} pending")
                if pending:
                    print("Reports saved; Slack delivery pending. Retry with ai-trend-radar notify.", file=sys.stderr)
                    return 2
            except (OSError, RuntimeError, sqlite3.Error):
                print("Reports saved; Slack delivery failed. Retry with ai-trend-radar notify.", file=sys.stderr)
                return 2
        return 0
    except (ConfigError, OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
        print(f"scan failed: {compact_error(exc)}", file=sys.stderr)
        return 1
