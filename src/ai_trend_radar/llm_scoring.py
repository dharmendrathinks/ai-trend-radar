"""Editorial topic priority, separate from deterministic Discovery Priority."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ai_trend_radar.llm_adapter import digest
from ai_trend_radar.models import isoformat

VERSION = "video-topic-v1"
WEIGHTS = {"developer_impact": 0.30, "demo_potential": 0.30, "audience_fit": 0.20, "freshness": 0.20}


def rank_updates(data: dict[str, Any], now: datetime, half_life_hours: float, audience: str) -> dict[str, Any]:
    """Recompute time-dependent scores on every report, even for cached extraction."""
    topics = []
    for release in data.get("updates", []):
        time_basis = release.get("time_basis", "release publication time")
        freshness = None
        freshness_reason = "Release publication date missing or invalid; freshness and overall priority unavailable."
        try:
            published = datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))
            age = (now - published).total_seconds() / 3600
            if age >= 0:
                freshness = 100 * 2 ** (-age / half_life_hours)
                freshness_reason = f"{time_basis.capitalize()}: {age:.1f} hours ago; calculated with a {half_life_hours:g}-hour half-life."
            else:
                freshness_reason = "Publication date is in the future; freshness and overall priority unavailable."
        except (KeyError, TypeError, ValueError, AttributeError):
            pass
        for angle in release["angles"]:
            editorial = angle.get("editorial") or {}
            categories = {}
            for name in ("developer_impact", "demo_potential", "audience_fit"):
                value = editorial.get(name, {})
                score, reason = value.get("score"), value.get("reason")
                valid = type(score) is int and 0 <= score <= 100 and isinstance(reason, str) and bool(reason.strip())
                categories[name] = {"score": score if valid else None,
                    "reason": reason if valid else "Editorial assessment unavailable; refresh extraction to score this topic.",
                    "basis": "LLM editorial judgment"}
            categories["freshness"] = {"score": round(freshness, 1) if freshness is not None else None,
                "reason": freshness_reason, "basis": "calculated from " + time_basis}
            complete = all(c["score"] is not None for c in categories.values())
            overall = round(sum(categories[name]["score"] * weight for name, weight in WEIGHTS.items()), 1) if complete else None
            topics.append({**{key: value for key, value in release.items() if key != "angles"}, **angle,
                "topic_id": digest([release["source_url"], angle["title"], angle["evidence_quotes"]])[:16],
                "release_title": release["title"],
                "video_priority": {"overall": overall, "categories": categories, "status": "scored" if complete else "unscored"}})
    # Stable global ordering, not grouped by release. Missing scores always go last.
    topics.sort(key=lambda t: (-(t["video_priority"]["overall"] if t["video_priority"]["overall"] is not None else -1),
        -(t["video_priority"]["categories"]["freshness"]["score"] or 0), t["source_url"], t["title"], t["topic_id"]))
    for index, topic in enumerate(topics, 1):
        topic["rank"] = index
    return {**data, "ranked_topics": topics, "ranking": {"version": VERSION, "weights": dict(WEIGHTS),
        "audience": audience, "assessed_at": isoformat(now), "freshness_half_life_hours": half_life_hours,
        "order": "overall descending, freshness descending, source URL, title, topic ID; unscored last",
        "limitations": "Editorial research priority, not predicted views or demand. Demo feasibility is untested; YouTube competition is not assessed."}}
