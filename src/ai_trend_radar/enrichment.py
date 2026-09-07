"""Optional, cached release and community-page extraction with isolated scoring."""
from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import shutil
from typing import Any

from ai_trend_radar import llm_adapter as adapter
from ai_trend_radar.config import AppConfig
from ai_trend_radar.models import SourceItem, isoformat
from ai_trend_radar.page_evidence import fetch_page, PageUnavailable
from ai_trend_radar.reports import _atomic_write

LOGGER = logging.getLogger(__name__)


def discover_updates(items: list[SourceItem], config: AppConfig, *, enabled: bool,
                     community_items: list[SourceItem] | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "disabled" if not enabled else "ok", "model": config.llm.model,
        "new_calls": 0, "cached_results": 0, "failures": 0, "abstentions": 0,
        "skipped": 0, "updates": [], "releases": [], "warnings": [],
    }
    if not enabled:
        return report
    # A model/cache failure must never prevent the deterministic report being saved.
    try:
        _discover(items, config, report, community_items or [])
    except Exception as exc:
        LOGGER.warning("LLM enrichment unavailable (%s)", type(exc).__name__)
        report["warnings"].append(f"Enrichment unavailable ({type(exc).__name__}); regular results are unaffected.")
        report["status"] = "partial" if report["updates"] else "unavailable"
    return report


def _discover(items: list[SourceItem], config: AppConfig, report: dict[str, Any], community_items: list[SourceItem]) -> None:
    settings = asdict(config.llm)
    settings["editorial_scoring"] = True
    base_identity = {
        "adapter": adapter.ADAPTER_VERSION,
        "adapter_sha256": adapter.digest(Path(adapter.__file__).read_text()),
        "schema_sha256": adapter.digest(adapter.read(adapter.HERE / "scored.schema.json")),
        "editorial_prompt_sha256": adapter.digest((adapter.HERE / "editorial.md").read_text()),
        "audience": settings["audience"],
        "model": settings["model"], "reasoning_effort": settings["reasoning_effort"],
    }
    cache_dir = config.database_path.with_suffix(".llm")
    binary = shutil.which(settings["codex_binary"])
    seen: set[tuple[str, str]] = set()
    consecutive_failures = 0
    accepted = 0
    community_attempted = 0
    releases = sorted(
        (item for item in items if item.item_type == "github_release"),
        key=lambda item: (isoformat(item.published_at or item.updated_at) or "", item.canonical_url),
        reverse=True,
    )
    community = [item for item in community_items if item.item_type == "hacker_news_story"] if settings["max_hn_stories"] else []
    report["coverage"] = {"github_release_candidates": len(releases), "hn_main_list_candidates": len(community),
        "hn_page_limit": settings["max_hn_stories"], "selection": "Main-list HN stories in Discovery Priority order first, then eligible GitHub releases newest first; one shared model-call budget. Other source types are not assessed."}
    for item in [*community, *releases]:
        is_community = item.item_type == "hacker_news_story"
        kind = "hacker_news_page" if is_community else "github_release"
        seen_key = (kind, item.canonical_url)
        if seen_key in seen:
            continue
        seen.add(seen_key)
        settings["source_kind"] = kind
        identity = {**base_identity, "source_kind": kind,
            "prompt_sha256": adapter.digest((adapter.HERE / ("community.md" if is_community else "prompt.md")).read_text())}
        entry = {"source_url": item.canonical_url, "title": item.title,
                 "source_kind": kind,
                 "time_basis": "HN submission time, not product launch time" if is_community else "release publication time",
                 "discovery_url": f"https://news.ycombinator.com/item?id={item.external_id}" if is_community else item.canonical_url,
                 "published_at": isoformat(item.published_at), "captured_at": isoformat(item.body_fetched_at),
                 "source_cache_state": item.cache_state,
                 "repository": item.metrics.get("repo_full_name", ""), "tag": item.metrics.get("release_tag", "")}
        report["releases"].append(entry)
        reason = None
        notes = item.full_text
        if is_community:
            if community_attempted >= settings["max_hn_stories"]:
                reason = "HN page limit"
            elif not binary:
                reason = "Codex executable unavailable; page not fetched"
            elif report["new_calls"] >= settings["max_calls_per_scan"] or consecutive_failures >= 3:
                reason = "model budget or failure limit reached; page not fetched"
            else:
                community_attempted += 1
                try:
                    page = fetch_page(item.canonical_url, config)
                    notes = page["text"]
                    entry.update(source_url=page["final_url"], captured_at=page["fetched_at"],
                        source_cache_state=page["cache_state"], evidence_basis="captured public-page text; not independently tested")
                except Exception as exc:
                    reason = str(exc) if isinstance(exc, PageUnavailable) else f"Linked page unavailable ({type(exc).__name__})"
        elif not item.canonical_url.startswith("https://github.com/"):
            reason = "unsupported source URL"
        elif not item.content_complete or item.full_text is None:
            reason = "incomplete release notes"
        elif len(item.full_text) > settings["max_input_chars"]:
            reason = "input character limit"
        elif accepted >= settings["max_releases"]:
            reason = "release limit"
        if reason:
            entry.update(status="skipped", reason=reason)
            report["skipped"] += 1
            continue
        if not is_community:
            accepted += 1
        evidence = {key: entry[key] for key in ("title", "source_url", "repository", "tag")}
        evidence["notes"] = notes
        key = adapter.digest({"evidence": evidence, "identity": identity})
        path = cache_dir / f"{key}.json"
        record = None
        if path.is_file():
            try:
                saved = adapter.read(path)
                candidate = saved["result"]
                if saved["key"] != key or saved["identity"] != identity or saved["evidence"] != evidence:
                    raise ValueError("cache identity mismatch")
                if candidate["status"] not in {"valid", "failed"}:
                    raise ValueError("invalid cache status")
                if candidate["status"] == "valid":
                    _, errors = adapter.validate_model(candidate["response"], evidence, scored=True)
                    if errors or candidate.get("errors") or candidate.get("tool_items"):
                        raise ValueError("invalid cached extraction")
                record = candidate
                report["cached_results"] += 1
                entry["cached"] = True
            except (OSError, ValueError, KeyError, TypeError):
                report["warnings"].append(f"Ignored invalid cache for {item.canonical_url}.")
        if record is None:
            if not binary:
                reason = "Codex executable unavailable; install/login or set llm.codex_binary"
            elif report["new_calls"] >= settings["max_calls_per_scan"]:
                reason = "model call limit"
            elif consecutive_failures >= 3:
                reason = "stopped after three consecutive model failures"
            if reason:
                entry.update(status="skipped", reason=reason)
                report["skipped"] += 1
                continue
            LOGGER.info("LLM extracting %s", item.title)
            report["new_calls"] += 1
            entry["cached"] = False
            try:
                record = adapter.model_result(evidence, settings, binary)
            except Exception as exc:
                record = {"status": "failed", "errors": [f"Model invocation failed ({type(exc).__name__})"], "response": None}
            consecutive_failures = consecutive_failures + 1 if record["status"] == "failed" else 0
            try:
                _atomic_write(path, json.dumps({"key": key, "identity": identity, "evidence": evidence,
                    "created_at": isoformat(datetime.now(UTC)), "result": record}, indent=2, ensure_ascii=False) + "\n")
            except OSError:
                report["warnings"].append(f"Could not save cache for {item.canonical_url}; a later scan may call the model again.")
        entry.update(status=record["status"], cache_key=key)
        if record["status"] != "valid":
            report["failures"] += 1
            entry["errors"] = record.get("errors", [])
            continue
        response = record["response"]
        if not response["angles"]:
            report["abstentions"] += 1
            entry["abstain_reason"] = response["abstain_reason"]
        else:
            report["updates"].append({**entry, "angles": response["angles"]})
    if report["failures"] or report["skipped"] or report["warnings"]:
        report["status"] = "partial" if report["new_calls"] or report["cached_results"] else "unavailable"
