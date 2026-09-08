"""Bounded developer assessment and source-anchored developments, not video scoring."""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import asdict
from datetime import datetime
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import urlsplit

from ai_trend_radar import llm_adapter as adapter
from ai_trend_radar.models import Candidate, isoformat
from ai_trend_radar.page_evidence import PageText, PageUnavailable, cached_page, fetch_page
from ai_trend_radar.reports import _atomic_write, _candidate_dict
from ai_trend_radar.resolution import occurrence_key
from ai_trend_radar.topics import _meaningful_change, _release_bullets, DEFAULT_NOISE_SECTIONS, DEFAULT_NOISE_TERMS, DEFAULT_CAPABILITY_TERMS, DEFAULT_DEVELOPER_TERMS, DEFAULT_HIGH_IMPACT_TERMS

VERSION = "developer-priority-v1"
WEIGHTS = {"developer_impact": .5, "developer_relevance": .3, "urgency": .2}
LABELS = {"developer_impact": "Developer impact", "developer_relevance": "Developer relevance", "urgency": "Urgency"}
LOG = logging.getLogger(__name__)


def normalized(text):
    return re.sub(r"\s+", " ", text).strip()


def evidence_blocks(text, source_id, source_url, evidence_basis):
    # Block identity ignores whitespace and position, not substantive source edits.
    blocks = {}
    paragraphs = re.split(r"\n\s*\n|\n(?=\s*(?:[-*] |#{1,6} ))", text)
    parts = []
    for paragraph in paragraphs:
        # Extracted HTML can contain many paragraphs separated by single newlines.
        # Avoid one giant anchor covering several unrelated changes or code samples.
        units = paragraph.splitlines() if len(paragraph) > 1200 else [paragraph]
        for unit in units:
            parts.extend(re.split(r'(?<=[.!?])\s+', unit) if len(unit) > 1200 else [unit])
    for part in parts:
        part = part.strip()
        if len(part) < 12:
            continue
        key = adapter.digest([source_id, normalized(part)])[:24]
        blocks[key] = {"evidence_id": key, "text": part, "source_url": source_url,
                       "source_id": source_id, "evidence_basis": evidence_basis}
    return list(blocks.values())


def primary_item(candidate):
    return min((i for i in candidate.items if i.evidence_role == "event"),
               key=lambda i: (i.item_type != "github_release", i.source_family != "official", occurrence_key(i)))


def fair_candidates(candidates):
    lanes = {}
    for candidate in sorted(candidates, key=lambda c: (-c.discovery_priority, c.fingerprint)):
        lanes.setdefault(primary_item(candidate).source_family, deque()).append(candidate)
    while any(lanes.values()):
        for family in sorted(lanes):
            if lanes[family]:
                yield lanes[family].popleft()


def document_url(item):
    parts = urlsplit(item.canonical_url)
    segments = [p for p in parts.path.split("/") if p]
    if parts.hostname == "github.com" and len(segments) == 2:
        return f"https://raw.githubusercontent.com/{'/'.join(segments)}/HEAD/README.md"
    if parts.hostname == "huggingface.co" and (len(segments) == 2 or (len(segments) == 3 and segments[0] == "spaces")):
        return f"https://huggingface.co/{'/'.join(segments)}/raw/main/README.md"
    return item.canonical_url


def captured_text(item):
    if not item.full_text or not item.content_complete:
        return None
    if item.content_format == "html":
        parser = PageText()
        parser.feed(item.full_text)
        return parser.text()
    return item.full_text


def source_document(item, config, coverage, *, allow_fetch=True, document_snapshot=None):
    text = captured_text(item)
    url = item.canonical_url
    basis = "publisher statement" if item.authority == "official" or item.source_family in {"official", "github", "huggingface"} else "linked-page statement; not independently verified"
    fetched_at = isoformat(item.body_fetched_at or item.observed_at)
    cache_state = item.cache_state
    if not text:
        if document_snapshot is not None:
            page = document_snapshot.get(document_url(item))
            if page is None:
                raise PageUnavailable('document not captured in replay; no network fetch')
        else:
            page = cached_page(document_url(item), config)
        if page is None:
            if not allow_fetch:
                raise PageUnavailable("model unavailable or call/failure limit; no extra document fetched")
            if coverage["page_fetches"] >= config.llm.max_page_fetches:
                raise PageUnavailable("additional document budget exhausted")
            coverage["page_fetches"] += 1
            page = fetch_page(document_url(item), config)
        text, url = page["text"], page["final_url"]
        fetched_at, cache_state = page["fetched_at"], page["cache_state"]
    if not 20 <= len(text) <= config.llm.max_input_chars:
        raise PageUnavailable("source text insufficient or exceeds input limit; no headline-only assessment")
    return {"source_id": occurrence_key(item), "source_url": url, "text": text,
            "evidence_basis": basis, "captured_at": fetched_at, "cache_state": cache_state}


def priority(editorial=None):
    return {"version": VERSION, "weights": dict(WEIGHTS),
            "overall": round(sum(editorial[k]["score"] * w for k, w in WEIGHTS.items()), 1) if editorial else None,
            "categories": editorial or {k: {"score": None, "reason": "Not assessed; N/A does not mean low usefulness."} for k in WEIGHTS}}


def sort_key(topic):
    p = topic["developer_priority"]
    time = topic.get("event_time")
    try:
        timestamp = datetime.fromisoformat(time.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, AttributeError):
        timestamp = 0
    if p["overall"] is None:
        return (1, -topic["discovery_priority"], 0, 0, 0, -timestamp, topic["topic_id"])
    return (0, -p["overall"], *(-p["categories"][k]["score"] for k in WEIGHTS), -timestamp, topic["topic_id"])


def base_topic(candidate, now):
    value = _candidate_dict(candidate, now, [])
    for key in ("video_topic", "topicability", "youtube_evidence", "presentation_gate"):
        value.pop(key, None)
    item = primary_item(candidate)
    value.update(parent_event_ids=[candidate.fingerprint], parent_title=candidate.title,
                 source_url=item.canonical_url, related_topic_ids=[],
                 assessment_status="unassessed", assessed_at=None, caveats=[], evidence_quotes=[],
                 evidence_type="source metadata; not an impact assessment", next_step="",
                 what_changed=item.summary or item.title, who_should_care="Not yet assessed.",
                 practical_difference="Not yet assessed; inspect the source before drawing conclusions.",
                 developer_priority=priority())
    if item.item_type == "hacker_news_story":
        value["event_time_basis"] = "HN submission time, not product launch time"
    return value


def placeholder(candidate, now, reason, *, watch=False):
    value = base_topic(candidate, now)
    key = adapter.digest([candidate.fingerprint, "unassessed-placeholder"])[:24]
    value.update(topic_id=key, event_id=key, fingerprint=key, source_anchor="placeholder",
                 is_placeholder=True, assessment_reason=reason, disposition="watch" if watch else "main")
    return value


def deterministic_topics(candidate, now, config):
    """Consequence extraction without inventing who/impact/urgency assessments."""
    item = primary_item(candidate)
    if item.item_type != "github_release":
        return []
    text = captured_text(item)
    if not text:
        return []
    options = {"noise_sections": DEFAULT_NOISE_SECTIONS, "noise_terms": DEFAULT_NOISE_TERMS,
               "capability_terms": DEFAULT_CAPABILITY_TERMS, "developer_terms": DEFAULT_DEVELOPER_TERMS,
               "high_impact_terms": DEFAULT_HIGH_IMPACT_TERMS}
    options.update({k: config.topics[k] for k in options if k in config.topics})
    blocks = evidence_blocks(text, occurrence_key(item), item.canonical_url, "publisher statement")
    results, seen = [], set()
    for bullet in _release_bullets(text):
        if not _meaningful_change(bullet, **options):
            continue
        block = next((b for b in blocks if normalized(bullet["text"]) in normalized(b["text"])), None)
        if not block or block["evidence_id"] in seen:
            continue
        seen.add(block["evidence_id"])
        value = base_topic(candidate, now)
        key = adapter.digest([candidate.fingerprint, block["evidence_id"]])[:24]
        value.update(topic_id=key, event_id=key, fingerprint=key, source_anchor=block["evidence_id"],
                     document_hashes={occurrence_key(item): adapter.digest(normalized(text))},
                     is_placeholder=False, title=f"{candidate.entity or candidate.title}: {bullet['text'][:160]}",
                     what_changed=bullet["text"], display_title=bullet["text"], disposition="main",
                     evidence_quotes=[{"evidence_id": block["evidence_id"], "quote": block["text"], "source_url": block["source_url"]}],
                     evidence_type="publisher statement", assessment_reason="Deterministic source extraction; impact not assessed.")
        results.append(value)
        if len(results) == 3:
            break
    return results


def assess_candidates(candidates, config, now, *, enabled, document_snapshot=None):
    coverage = {"status": "ok" if enabled else "disabled", "model": config.llm.model,
                "candidate_events": len(candidates), "considered": 0, "page_fetches": 0,
                "new_calls": 0, "cached_results": 0, "failures": 0, "abstentions": 0,
                "skipped": 0, "entries": [], "warnings": [], "by_source_family": {},
                "selection": "Source-family round-robin; Discovery Priority within each family. Bounded coverage, not an exhaustive ranking."}
    topics, lanes = [], Counter()
    binary = shutil.which(config.llm.codex_binary)
    settings = {**asdict(config.llm), "developer_assessment": True, "audience": config.developer_audience}
    identity = {"version": VERSION, "model": config.llm.model, "reasoning": config.llm.reasoning_effort,
                "audience": config.developer_audience,
                "adapter": adapter.digest(Path(adapter.__file__).read_text()),
                "prompt": adapter.digest((adapter.HERE / "developer.md").read_text()),
                "schema": adapter.digest(adapter.read(adapter.HERE / "developer.schema.json"))}
    consecutive_failures = 0
    for index, candidate in enumerate(fair_candidates(candidates)):
        item = primary_item(candidate)
        family = item.source_family
        family_counts = coverage["by_source_family"].setdefault(family, {"candidates": 0, "considered": 0, "assessed": 0})
        family_counts["candidates"] += 1
        entry = {"parent_event_id": candidate.fingerprint, "title": candidate.title, "source_url": item.canonical_url,
                 "source_family": family, "status": "unassessed", "reason": ""}
        coverage["entries"].append(entry)
        record = None
        try:
            if not enabled:
                raise PageUnavailable("LLM disabled")
            if index >= config.llm.max_candidates:
                raise PageUnavailable("candidate assessment limit")
            coverage["considered"] += 1
            family_counts["considered"] += 1
            if item.item_type == "github_release" and lanes["release"] >= config.llm.max_releases:
                raise PageUnavailable("GitHub release limit")
            if family == "hacker_news" and lanes["hn"] >= config.llm.max_hn_stories:
                raise PageUnavailable("HN document limit")
            lanes["release" if item.item_type == "github_release" else "hn" if family == "hacker_news" else family] += 1
            # Complete captured evidence can still hit cache without an executable.
            allow_fetch = bool(binary and coverage["new_calls"] < config.llm.max_calls_per_scan and consecutive_failures < 3)
            document = source_document(item, config, coverage, allow_fetch=allow_fetch, document_snapshot=document_snapshot)
            documents = [document]
            remaining = config.llm.max_input_chars - len(document["text"])
            for support in sorted(candidate.items, key=occurrence_key):
                if support is item or support.evidence_role != "event" or len(documents) == 3:
                    continue
                text = captured_text(support)
                if text and 20 <= len(text) <= remaining:
                    documents.append({"source_id": occurrence_key(support), "source_url": support.canonical_url,
                                      "text": text, "evidence_basis": "collected supporting statement; not independent verification"})
                    remaining -= len(text)
            blocks = [block for doc in documents for block in evidence_blocks(doc["text"], doc["source_id"], doc["source_url"], doc["evidence_basis"])]
            evidence = {"title": candidate.title, "blocks": blocks, "event_time": isoformat(candidate.effective_event_time),
                        "time_basis": base_topic(candidate, now)["event_time_basis"]}
            key = adapter.digest([identity, evidence])
            entry['cache_key'] = 'developer-' + key
            path = config.database_path.with_suffix(".llm") / f"developer-{key}.json"
            try:
                saved = adapter.read(path)
                if saved["key"] != key or saved["identity"] != identity or saved["evidence"] != evidence:
                    raise ValueError("cache identity mismatch")
                record = saved["result"]
                if record["status"] == "valid":
                    _, errors = adapter.validate_developments(record["response"], evidence)
                    if errors or record.get("errors") or record.get("tool_items"):
                        raise ValueError("invalid cached response")
                elif record["status"] != "failed":
                    raise ValueError("invalid cache status")
                entry["assessed_at"] = saved["assessed_at"]
                coverage["cached_results"] += 1
            except FileNotFoundError:
                record = None
            except (OSError, ValueError, KeyError, TypeError):
                record = None
                coverage["warnings"].append(f"Invalid assessment cache ignored for {candidate.fingerprint}")
            if record is None:
                if not binary or coverage["new_calls"] >= config.llm.max_calls_per_scan or consecutive_failures >= 3:
                    raise PageUnavailable("model executable unavailable or call/failure limit")
                coverage["new_calls"] += 1
                LOG.info("Developer assessment: %s", candidate.title)
                try:
                    record = adapter.model_result(evidence, settings, binary)
                    if record["status"] == "valid":
                        _, errors = adapter.validate_developments(record["response"], evidence)
                        if errors or record.get("tool_items") or record.get("errors"):
                            raise ValueError("invalid model evidence")
                except Exception as exc:
                    record = {"status": "failed", "errors": [f"Assessment failed ({type(exc).__name__})"]}
                consecutive_failures = consecutive_failures + 1 if record["status"] == "failed" else 0
                entry["assessed_at"] = isoformat(now)
                try:
                    _atomic_write(path, json.dumps({"key": key, "identity": identity, "evidence": evidence,
                        "assessed_at": entry["assessed_at"], "result": record}, ensure_ascii=False) + "\n")
                except OSError:
                    coverage["warnings"].append("Assessment cache write failed; a later scan may repeat the call.")
            if record["status"] != "valid":
                coverage["failures"] += 1
                raise PageUnavailable("; ".join(record.get("errors", ["assessment failed"])))
            family_counts["assessed"] += 1
            if not record["response"]["developments"]:
                coverage["abstentions"] += 1
                entry.update(status="abstained", reason=record["response"]["abstain_reason"])
                topics.append(placeholder(candidate, now, entry["reason"], watch=True))
                continue
            entry["status"] = "assessed"
            block_map = {b["evidence_id"]: b for b in blocks}
            for development in record["response"]["developments"]:
                anchor = development["primary_evidence_id"]
                topic = base_topic(candidate, now)
                key = adapter.digest([candidate.fingerprint, anchor])[:24]
                score = priority(development["editorial"])
                eligible = score["overall"] >= 50 and development["editorial"]["developer_impact"]["score"] >= 25 and development["editorial"]["developer_relevance"]["score"] >= 50
                topic.update({k: v for k, v in development.items() if k not in {"editorial", "primary_evidence_id"}})
                topic.update(topic_id=key, event_id=key, fingerprint=key, source_anchor=anchor, is_placeholder=False,
                             document_hashes={doc['source_id']: adapter.digest(normalized(doc['text'])) for doc in documents},
                             display_title=development["title"], developer_priority=score, assessment_status="assessed",
                             assessed_at=entry["assessed_at"], assessment_reason="Source-grounded editorial judgment; not independent testing.",
                             disposition="main" if eligible else "watch",
                             evidence_quotes=[{**q, "source_url": block_map[q["evidence_id"]]["source_url"]} for q in development["evidence_quotes"]])
                topic["source_links"] = sorted(set(topic["source_links"]) | {q["source_url"] for q in topic["evidence_quotes"]})
                if not eligible:
                    topic['assessment_reason'] += ' Watch: below main-list floors (overall 50, impact 25, relevance 50).'
                topics.append(topic)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, PageUnavailable) else f"Assessment unavailable ({type(exc).__name__})"
            entry["reason"] = reason
            coverage["skipped"] += int(enabled)
            fallback = deterministic_topics(candidate, now, config)
            if fallback:
                for topic in fallback:
                    topic["assessment_reason"] = reason
                topics.extend(fallback)
            else:
                # A release with complete notes but no concrete consequence is Watch.
                watch = item.item_type == "github_release" and bool(captured_text(item))
                topics.append(placeholder(candidate, now, reason + ('; no concrete developer consequence extracted from release notes' if watch else ''), watch=watch))
    for topic in topics:
        topic["related_topic_ids"] = sorted(t["topic_id"] for t in topics if t["topic_id"] != topic["topic_id"] and t["parent_event_ids"] == topic["parent_event_ids"])
    if enabled and (coverage["skipped"] or coverage["failures"] or coverage["warnings"]):
        coverage["status"] = "partial"
    return sorted(topics, key=sort_key), coverage
