"""Optional, cached release-note extraction independent of deterministic rankings."""
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
from ai_trend_radar.reports import _atomic_write

LOGGER = logging.getLogger(__name__)


def discover_updates(items: list[SourceItem], config: AppConfig, *, enabled: bool) -> dict[str, Any]:
    report: dict[str, Any] = {
        "status": "disabled" if not enabled else "ok", "model": config.llm.model,
        "new_calls": 0, "cached_results": 0, "failures": 0, "abstentions": 0,
        "skipped": 0, "updates": [], "releases": [], "warnings": [],
    }
    if not enabled:
        return report
    # A model/cache failure must never prevent the deterministic report being saved.
    try:
        _discover(items, config, report)
    except Exception as exc:
        LOGGER.warning("LLM enrichment unavailable (%s)", type(exc).__name__)
        report["warnings"].append(f"Enrichment unavailable ({type(exc).__name__}); regular results are unaffected.")
        report["status"] = "partial" if report["updates"] else "unavailable"
    return report


def _discover(items: list[SourceItem], config: AppConfig, report: dict[str, Any]) -> None:
    settings = asdict(config.llm)
    identity = {
        "adapter": adapter.ADAPTER_VERSION,
        "adapter_sha256": adapter.digest(Path(adapter.__file__).read_text()),
        "prompt_sha256": adapter.digest((adapter.HERE / "prompt.md").read_text()),
        "schema_sha256": adapter.digest(adapter.read(adapter.HERE / "response.schema.json")),
        "model": settings["model"], "reasoning_effort": settings["reasoning_effort"],
    }
    cache_dir = config.database_path.with_suffix(".llm")
    binary = shutil.which(settings["codex_binary"])
    seen: set[str] = set()
    consecutive_failures = 0
    accepted = 0
    releases = sorted(
        (item for item in items if item.item_type == "github_release"),
        key=lambda item: (isoformat(item.published_at or item.updated_at) or "", item.canonical_url),
        reverse=True,
    )
    for item in releases:
        if item.canonical_url in seen:
            continue
        seen.add(item.canonical_url)
        entry = {"source_url": item.canonical_url, "title": item.title,
                 "published_at": isoformat(item.published_at), "captured_at": isoformat(item.body_fetched_at),
                 "source_cache_state": item.cache_state,
                 "repository": item.metrics.get("repo_full_name", ""), "tag": item.metrics.get("release_tag", "")}
        report["releases"].append(entry)
        reason = None
        if not item.canonical_url.startswith("https://github.com/"):
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
        accepted += 1
        evidence = {key: entry[key] for key in ("title", "source_url", "repository", "tag")}
        evidence["notes"] = item.full_text
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
                    _, errors = adapter.validate_model(candidate["response"], evidence)
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
