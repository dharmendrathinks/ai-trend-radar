from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any
import json
import os
import tempfile

from ai_trend_radar.config import AppConfig
from ai_trend_radar.llm_scoring import rank_updates
from ai_trend_radar.models import Candidate, ProviderResult, isoformat
from ai_trend_radar.ranking import SCORING_VERSION
from ai_trend_radar.resolution import effective_item_time


SCHEMA_VERSION = "2.3"


def _event_time_basis(candidate: Candidate) -> str:
    if any(i.item_type == "github_observed_growth" for i in candidate.items):
        return "observed growth interval ending"
    primary = min((i for i in candidate.items if i.evidence_role != "project_context"), key=effective_item_time)
    if primary.source_family == "huggingface" and primary.updated_at:
        return "source modification time"
    return "source publication time" if primary.published_at else ("source modification time" if primary.updated_at else "first seen locally; publication time unknown")


def _candidate_dict(candidate: Candidate, now: datetime, unavailable: list[str]) -> dict[str, Any]:
    age_hours = max(0.0, (now - candidate.effective_event_time).total_seconds() / 3600)
    display_title = (
        candidate.video_topic.get("primary_angle", {}).get("title")
        if candidate.video_topic
        else candidate.title
    )
    return {
        "event_id": candidate.fingerprint,
        "fingerprint": candidate.fingerprint,
        "event_time_basis": _event_time_basis(candidate),
        "title": candidate.title,
        "display_title": display_title or candidate.title,
        "video_topic": candidate.video_topic or None,
        "topicability": candidate.video_topic.get("topicability") if candidate.video_topic else None,
        "presentation_gate": candidate.presentation_gate or None,
        "entity": candidate.entity,
        "event_time": isoformat(candidate.effective_event_time),
        "age_hours": round(age_hours, 2),
        "discovery_priority": candidate.discovery_priority,
        "discovery_priority_inputs": {
            "freshness": candidate.freshness,
            "evidence_strength": candidate.evidence_value,
            "interest_value": candidate.interest_value,
        },
        "freshness": candidate.freshness,
        "evidence_level": candidate.evidence_level,
        "interest_band": candidate.interest_band,
        "interest_rule": candidate.interest_rule,
        "interest_inputs": candidate.interest_inputs,
        "source_families": candidate.source_families,
        "observed_signals": [item.to_dict() for item in candidate.items],
        "youtube_evidence": candidate.youtube,
        "why_investigate": (
            f"{age_hours:.1f}h old; {candidate.evidence_level}; "
            f"interest is {candidate.interest_band} because {candidate.interest_rule}."
        ),
        "missing_or_uncertain": list(dict.fromkeys([*candidate.missing, *unavailable, *(["Source content is incomplete or summary-only"] if any(not i.content_complete for i in candidate.items) else [])])),
        "source_links": sorted(
            {item.canonical_url for item in candidate.items}
            | {link for item in candidate.items for link in item.related_links}
        ),
    }


def build_report(
    *,
    scan_id: str,
    started_at: datetime,
    completed_at: datetime,
    status: str,
    config: AppConfig,
    provider_results: list[ProviderResult],
    candidates: list[Candidate],
    release_watch: list[Candidate] | None = None,
    community_watch: list[Candidate] | None = None,
    llm_updates: dict[str, Any] | None = None,
) -> dict[str, Any]:
    unavailable = [
        f"{result.provider}: {result.status}" + (f" ({result.error})" if result.error else "")
        for result in provider_results
        if result.status in {"failed", "disabled", "stale"}
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "scoring_version": SCORING_VERSION,
        "configuration_fingerprint": config.fingerprint,
        "scan_id": scan_id,
        "started_at": isoformat(started_at),
        "generated_at": isoformat(completed_at),
        "status": status,
        "llm_updates": rank_updates(llm_updates or {"status": "disabled", "updates": []}, completed_at,
                                    config.ranking.freshness_half_life_hours, config.llm.audience),
        "product_boundary": "YouTube evidence is not included in Discovery Priority; inspect it manually.",
        "provider_status": [result.status_dict() for result in provider_results],
        "effective_interest_thresholds": asdict(config.ranking.interest),
        "effective_eligibility_thresholds": asdict(config.ranking.eligibility),
        "recommendations": [_candidate_dict(candidate, completed_at, unavailable) for candidate in candidates],
        "release_watch": [
            _candidate_dict(candidate, completed_at, unavailable)
            for candidate in (release_watch or [])
        ],
        "community_watch": [
            _candidate_dict(candidate, completed_at, unavailable)
            for candidate in (community_watch or [])
        ],
    }


def _signal_time(signal: dict[str, Any]) -> str:
    if signal["item_type"] == "github_observed_growth":
        metrics = signal["metrics"]
        return f"observed interval {metrics.get('growth_interval_start', 'unknown')} to {metrics.get('growth_interval_end', signal['published_at'])}"
    if signal.get("published_at"):
        label = f"published {signal['published_at']}"
    elif signal.get("first_seen_at"):
        label = f"first seen {signal['first_seen_at']} (publication time unknown)"
    else:
        label = f"observed {signal['observed_at']}"
    if signal.get("last_confirmed_at"):
        label += f"; confirmed {signal['last_confirmed_at']} ({signal.get('cache_state', 'unknown')})"
    return label


def _score_text(value: int | float | None) -> str:
    return f"{value:g}" if value is not None else "N/A"


def _llm_markdown(data: dict[str, Any]) -> list[str]:
    lines = ["## LLM-discovered updates", ""]
    if data["status"] == "disabled":
        return [*lines, "Disabled. Enable with `scan --llm` or `[llm] enabled = true` (uses model allowance).", ""]
    ranked = "ranked_topics" in data
    order = "Ranked by Overall Priority, highest first; unscored topics last." if ranked else "Legacy report: newest release first; editorial scores unavailable."
    lines.extend([
        f"Status: **{data['status']}** · Model: `{data['model']}` · New calls: {data['new_calls']} · Cached: {data['cached_results']} · Failed: {data['failures']} · Skipped: {data['skipped']} · Abstained: {data['abstentions']}",
        "", f"Model-generated suggestions from captured GitHub release notes and selected Hacker News linked pages. {order} Quotes are checked against captured text; interpretations and publisher claims still need human review. These do not change Discovery Priority, the review inbox, or Slack briefs and have no topic-specific YouTube validation.", "",
    ])
    if data.get("coverage"):
        coverage = data["coverage"]
        lines.extend([f"Coverage: {coverage['github_release_candidates']} eligible GitHub releases; {coverage['hn_main_list_candidates']} main-list HN stories, up to {coverage['hn_page_limit']} linked pages. {coverage['selection']}", ""])
    topics = data.get("ranked_topics", [
        {**release, **angle, "release_title": release["title"]}
        for release in data["updates"] for angle in release["angles"]
    ])
    labels = {"developer_impact": "Developer impact", "demo_potential": "Demo potential",
              "freshness": "Freshness", "audience_fit": "Audience impact"}
    if ranked:
        ranking = data["ranking"]
        lines.extend([
            "**Every category is scored out of 100.** Developer impact, demo potential, and audience impact are LLM editorial judgments, not measured outcomes. Audience impact assesses relevance to the configured audience, not predicted reach. Freshness uses release publication time or HN submission time; a recent HN post does not prove a new product. Overall = 30% developer impact + 30% demo + 20% freshness + 20% audience impact.",
            "", f"Audience: {ranking['audience']}",
            "", f"Rubric: `{ranking['version']}` · Freshness half-life: {ranking['freshness_half_life_hours']:g} hours. {ranking['limitations']}", "",
        ])
        if topics:
            lines.extend(["| # | Topic | Developer impact /100 | Demo potential /100 | Freshness /100 | Audience impact /100 | Overall /100 |",
                          "|---:|---|---:|---:|---:|---:|---:|",])
            for number, topic in enumerate(topics, 1):
                priority = topic["video_priority"]
                title = topic["title"].replace("|", "\\|").replace("\n", " ")
                values = [*[_score_text(priority["categories"][key]["score"]) for key in labels], _score_text(priority["overall"])]
                lines.append(f"| {number} | {title} | " + " | ".join(values) + " |")
            lines.append("")
    if not topics:
        lines.extend(["No validated LLM-discovered updates available for this scan.", ""])
    for number, topic in enumerate(topics, 1):
        lines.extend([f"### {number}. {topic['title']}", "",
            f"{'Linked page' if topic.get('source_kind') == 'hacker_news_page' else 'Release'}: [{topic['release_title']}]({topic['source_url']}) · {topic.get('time_basis', 'release publication time')}: {topic['published_at'] or 'unknown'} · {'Cached extraction' if topic['cached'] else 'New extraction'} · Source: {topic['source_cache_state']}", "",
        ])
        if topic.get("source_kind") == "hacker_news_page":
            lines.extend([f"[HN discussion]({topic['discovery_url']}) · Evidence: extracted public-page text, not independent product testing.", ""])
        if ranked:
            priority = topic["video_priority"]
            lines.extend([f"**Overall Priority: {_score_text(priority['overall'])}/100**", ""])
            for key, label in labels.items():
                category = priority["categories"][key]
                lines.append(f"- **{label}: {_score_text(category['score'])}/100** — {category['reason']}")
            lines.append("")
        lines.extend([topic["developer_value"], "", "Source evidence:", ""])
        for quote in topic["evidence_quotes"]:
            lines.extend(["> " + quote.replace("\r\n", "\n").replace("\n", "\n> "), ""])
        if topic["caveats"]:
            lines.extend(["Caveats:", "", *[f"- {caveat}" for caveat in topic["caveats"]], ""])
    if data["failures"] or data["skipped"] or data["warnings"]:
        lines.extend(["Extraction limitations (regular results remain available):", ""])
        for entry in data["releases"]:
            if entry.get("status") in {"failed", "skipped"}:
                reason = entry.get("reason") or "; ".join(entry.get("errors", []))
                lines.append(f"- [{entry['title']}]({entry['source_url']}): {entry['status']} — {reason}")
        lines.extend([*[f"- {warning}" for warning in data["warnings"]], ""])
    return lines


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# AI Trend Radar",
        "",
        f"Generated: {report['generated_at']}",
        f"Scan: `{report['scan_id']}` · Status: **{report['status']}** · Scoring: `{report['scoring_version']}`",
        "",
        f"> {report['product_boundary']}",
        "",
        *_llm_markdown(report.get("llm_updates", {"status": "disabled", "updates": []})),
        "## Provider status",
        "",
        "| Provider | Status | Items | Requests | Detail |",
        "|---|---:|---:|---:|---|",
    ]
    for provider in report["provider_status"]:
        detail = provider.get("error") or (f"stale as of {provider['stale_as_of']}" if provider.get("stale_as_of") else "—")
        safe_detail = str(detail).replace("|", "\\|")
        lines.append(f"| {provider['provider']} | {provider['status']} | {provider['item_count']} | {provider['request_count']} | {safe_detail} |")

    for provider in report["provider_status"]:
        tracking = provider.get("details", {}).get("established_tracking")
        if tracking:
            lines.extend(["", f"Established repository follow-up: {tracking['active']}/{tracking['limit']} slots; {tracking['sampled']} samples returned this scan (may include cached/stale data). Recent activity starts observation, not a trend claim. Qualifying measured growth appears as its own event."])

    lines.extend(["", f"## Top Opportunities — {len(report['recommendations'])} found", ""])
    if not report["recommendations"]:
        lines.extend(["No relevant candidates were found in the configured lookback window.", ""])
    for index, candidate in enumerate(report["recommendations"], start=1):
        entity = f" · {candidate['entity']}" if candidate.get("entity") else ""
        topic = candidate.get("video_topic") or {}
        lines.extend(
            [
                f"### {index}. {candidate.get('display_title') or candidate['title']}",
                "",
                f"Event: `{candidate.get('event_id', candidate['fingerprint'])}` · Time basis: {candidate.get('event_time_basis', 'source timestamp')}",
                "",
            ]
        )
        if topic:
            release_time = f" · {topic['release_published_at']}" if topic.get("release_published_at") else ""
            lines.extend(
                [
                    f"**Release:** [{topic['release_title']}]({topic['release_url']}){release_time}",
                    "",
                    f"**Topic extraction:** specificity={topic['specificity']} · `{topic['extraction_version']}`",
                    "",
                    "**Potential video angles:**",
                    "",
                ]
            )
            angles = [topic["primary_angle"], *topic.get("alternative_angles", [])]
            for angle_index, angle in enumerate(angles):
                label = "Primary" if angle_index == 0 else "Alternative"
                lines.append(f"- **{label}:** {angle['title']}")
                evidence = angle.get("evidence")
                if evidence:
                    section = f" ({evidence['section']})" if evidence.get("section") else ""
                    lines.append(f"  - Release-note evidence{section}: {evidence['text']}")
            if topic.get("fallback_reason"):
                lines.append(f"- Fallback: {topic['fallback_reason']}")
            lines.append("")
        lines.extend(
            [
                f"**Discovery Priority:** {candidate['discovery_priority']:.2f} · **Freshness:** {candidate['freshness']:.2f} · **Age:** {candidate['age_hours']:.1f}h{entity}",
                "",
                f"**Why investigate:** {candidate['why_investigate']}",
                "",
                f"**Evidence:** {candidate['evidence_level']} · **Interest:** {candidate['interest_band']} (`{candidate['interest_rule']}`)",
                "",
                "Observed signals:",
                "",
            ]
        )
        for signal in candidate["observed_signals"]:
            metric_parts = []
            for key, value in signal["metrics"].items():
                if key == "observed_growth":
                    continue
                if isinstance(value, (str, int, float)) and value not in ("", None):
                    metric_parts.append(f"{key}={value}")
            metrics = f" ({', '.join(metric_parts[:6])})" if metric_parts else ""
            role = "Project context (not release interest): " if signal.get("evidence_role") == "project_context" else ""
            lines.append(f"- {role}[{signal['provider']}] [{signal['title']}]({signal['canonical_url']}) — {_signal_time(signal)}{metrics}")

        youtube = candidate["youtube_evidence"]
        lines.extend(["", f"YouTube evidence ({youtube.get('status', 'not checked')} — not included in Discovery Priority):", ""])
        viewer_intent = youtube.get("viewer_intent", {})
        if viewer_intent:
            lines.append(
                "- Viewer intent: "
                f"{viewer_intent.get('type', 'event')} · specificity={viewer_intent.get('specificity', 'unknown')}"
                f" · {viewer_intent.get('basis', 'no basis recorded')}"
            )
        queries = youtube.get("queries", [])
        api_queries = youtube.get("api_queries", queries)
        urls = youtube.get("manual_search_urls", [])
        for index, query in enumerate(queries):
            api_query = api_queries[index] if index < len(api_queries) else query
            url = urls[index] if index < len(urls) else ""
            if api_query != query:
                lines.append(f"- Viewer intent: `{query}`")
                lines.append(f"  - YouTube API query: [{api_query}]({url})")
            else:
                lines.append(f"- Exact YouTube search: [{query}]({url})")
        if youtube.get("videos"):
            lines.append(
                f"- Returned results: {len(youtube['videos'])} shown below in YouTube order; "
                "judge intent relevance manually."
            )
        annotations = youtube.get("local_relevance_annotations", {})
        if annotations:
            policy_url = annotations.get("policy_url", "")
            policy_link = f" ([policy]({policy_url}))" if policy_url else ""
            lines.append(
                "- Client-generated relevance annotations: "
                f"{annotations.get('status', 'disabled')} — {annotations.get('reason', 'no reason recorded')}"
                f"{policy_link}. YouTube result order and content are preserved."
            )
        for video in youtube.get("videos", []):
            views = f" · {video['views']:,} views" if video.get("views") is not None else ""
            annotation = video.get("local_relevance")
            local_label = f" · Client annotation: {annotation['label']}" if annotation else ""
            lines.append(
                f"- [{video['title']}]({video['url']}) — "
                f"{video['channel']} · {video['published_at']}{views}{local_label}"
            )
        if youtube.get("reason"):
            lines.append(f"- Unavailable: {youtube['reason']}")
        for error in youtube.get("errors", []):
            lines.append(f"- Error: {error}")

        if candidate.get("missing_or_uncertain"):
            lines.extend(["", "Missing or uncertain:", ""])
            lines.extend(f"- {value}" for value in candidate["missing_or_uncertain"])
        lines.append("")

    lines.extend(["## Release Watch", ""])
    lines.extend(
        [
            "Release and authoritative changelog events retained outside the main list because no clear "
            "standalone video angle was extracted or the configured main-list floor was not met.",
            "Their underlying Discovery Priority is preserved.",
            "",
        ]
    )
    if not report.get("release_watch"):
        lines.extend(["No releases were withheld for low topicability.", ""])
    for index, candidate in enumerate(report.get("release_watch", []), start=1):
        topic = candidate.get("video_topic") or {}
        topicability = candidate.get("topicability") or {}
        gate = candidate.get("presentation_gate") or {}
        entity = f" · {candidate['entity']}" if candidate.get("entity") else ""
        lines.extend(
            [
                f"### R{index}. {candidate.get('display_title') or candidate['title']}",
                "",
                f"**Release:** [{topic.get('release_title', candidate['title'])}]({topic.get('release_url', candidate['source_links'][0])})",
                "",
                f"**Watch reason:** {gate.get('reason') or topicability.get('reason', 'no clear standalone video angle')}",
                "",
                f"**Topic extraction:** specificity={topic.get('specificity', 'low')} · `{topic.get('extraction_version', 'unknown')}`",
                "",
                f"**Discovery Priority:** {candidate['discovery_priority']:.2f} · **Freshness:** {candidate['freshness']:.2f} · **Age:** {candidate['age_hours']:.1f}h{entity}",
                "",
                f"**Evidence:** {candidate['evidence_level']} · **Interest:** {candidate['interest_band']} (`{candidate['interest_rule']}`)",
                "",
            ]
        )
        if topic.get("fallback_reason"):
            lines.extend([f"**Fallback reason:** {topic['fallback_reason']}", ""])
        lines.extend(["Observed signals:", ""])
        for signal in candidate["observed_signals"]:
            lines.append(
                f"- [{signal['provider']}] [{signal['title']}]({signal['canonical_url']}) — "
                f"{_signal_time(signal)}"
            )
        lines.append("")

    lines.extend(["## Community Watch", ""])
    lines.extend(
        [
            "Relevant community discoveries retained outside the English-oriented main list because "
            "they lack sufficient promotion evidence, lack a substantial Latin-script supporting source, "
            "or remained weak and stagnant after enough observation history.",
            "Their underlying Discovery Priority is preserved.",
            "",
        ]
    )
    if not report.get("community_watch"):
        lines.extend(["No community candidates were withheld by presentation gates.", ""])
    for index, candidate in enumerate(report.get("community_watch", []), start=1):
        gate = candidate.get("presentation_gate") or {}
        entity = f" · {candidate['entity']}" if candidate.get("entity") else ""
        lines.extend(
            [
                f"### C{index}. {candidate.get('display_title') or candidate['title']}",
                "",
                f"**Presentation gate:** `{gate.get('gate', 'unknown')}` · {gate.get('reason', 'no reason recorded')}",
                "",
                f"**Discovery Priority:** {candidate['discovery_priority']:.2f} · **Freshness:** {candidate['freshness']:.2f} · **Age:** {candidate['age_hours']:.1f}h{entity}",
                "",
                f"**Evidence:** {candidate['evidence_level']} · **Interest:** {candidate['interest_band']} (`{candidate['interest_rule']}`)",
                "",
                f"**Gate measurements:** `{json.dumps(gate.get('measurements', {}), sort_keys=True, ensure_ascii=False)}`",
                "",
                "Observed signals:",
                "",
            ]
        )
        for signal in candidate["observed_signals"]:
            lines.append(
                f"- [{signal['provider']}] [{signal['title']}]({signal['canonical_url']}) — "
                f"{_signal_time(signal)}"
            )
        lines.append("")

    lines.extend(
        [
            "## Scoring method",
            "",
            "`Discovery Priority = 0.60 × Freshness + 0.25 × Evidence Strength + 0.15 × Interest Value`",
            "",
            "Eligibility and interest thresholds are configuration-driven starting heuristics. The JSON report records their effective values and raw inputs.",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def write_reports(report: dict[str, Any], directory: Path) -> tuple[Path, Path]:
    stamp = str(report["generated_at"]).replace("-", "").replace(":", "").replace("+", "")
    base = f"scan-{stamp}-{report['scan_id']}"
    json_path = directory / f"{base}.json"
    markdown_path = directory / f"{base}.md"
    json_content = json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    markdown_content = render_markdown(report)
    _atomic_write(json_path, json_content)
    _atomic_write(markdown_path, markdown_content)
    _atomic_write(directory / "latest.json", json_content)
    _atomic_write(directory / "latest.md", markdown_content)
    return markdown_path, json_path
