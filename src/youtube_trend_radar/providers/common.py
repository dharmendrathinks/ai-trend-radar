from __future__ import annotations

from datetime import datetime

from youtube_trend_radar.models import ProviderStatus


def combined_status(*, item_count: int, failures: int, cache_states: list[str]) -> ProviderStatus:
    if item_count == 0 and failures:
        return "failed"
    if failures:
        return "partial"
    if any(state == "stale" for state in cache_states):
        return "stale"
    if cache_states and all(state != "live" for state in cache_states):
        return "cached"
    return "ok"


def oldest_stale_at(cache_states: list[tuple[str, datetime]]) -> datetime | None:
    stale = [fetched_at for state, fetched_at in cache_states if state == "stale"]
    return min(stale) if stale else None



def apply_provenance(item, payload):
    """Carry acquisition/validation clocks without changing local discovery time."""
    item.body_fetched_at = payload.fetched_at
    item.last_confirmed_at = payload.confirmed_at or payload.fetched_at
    item.cache_state = payload.cache_state
    return item


MAX_DOCUMENT_CHARACTERS = 1_000_000


def capture_document(item, text: str, content_format: str, *, complete: bool = True):
    item.full_text = text[:MAX_DOCUMENT_CHARACTERS]
    item.content_format = content_format
    item.content_complete = complete and len(text) <= MAX_DOCUMENT_CHARACTERS
    return item
