from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
import os

from youtube_trend_radar.config import AppConfig
from youtube_trend_radar.discovery import RepositoryDiscovery
from youtube_trend_radar.http import CachedHttpClient
from youtube_trend_radar.models import ProviderResult, SourceItem
from youtube_trend_radar.providers.common import apply_provenance, capture_document, combined_status, oldest_stale_at
from youtube_trend_radar.resolution import is_relevant
from youtube_trend_radar.utils import clean_text, compact_error, normalize_url, parse_datetime


API = "https://api.github.com"


def build_client(config: AppConfig, database: Any) -> CachedHttpClient:
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN")
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return CachedHttpClient(database, config.http, default_headers=headers, secrets=[token] if token else [])


def _repo_item(repo: dict[str, Any], *, provider: str, item_type: str, now: datetime) -> SourceItem:
    full_name = str(repo["full_name"])
    return SourceItem(
        provider=provider,
        external_id=full_name.lower(),
        source_family="github",
        item_type=item_type,
        title=f"{full_name}: {clean_text(repo.get('description'), limit=220) or 'repository activity'}",
        summary=clean_text(repo.get("description")),
        canonical_url=normalize_url(str(repo["html_url"])),
        published_at=parse_datetime(repo.get("created_at")),
        updated_at=parse_datetime(repo.get("pushed_at") or repo.get("updated_at")),
        observed_at=now,
        entity=full_name.split("/", 1)[0],
        categories=[str(topic).lower() for topic in repo.get("topics", [])],
        metrics={
            "stars": int(repo.get("stargazers_count", 0)),
            "forks": int(repo.get("forks_count", 0)),
            "open_issues": int(repo.get("open_issues_count", 0)),
            "repo_full_name": full_name,
            "repository_created_at": repo.get("created_at"),
        },
    )


def collect_watched(config: AppConfig, client: CachedHttpClient, now: datetime) -> ProviderResult:
    items: list[SourceItem] = []
    failures: list[str] = []
    cache_states: list[tuple[str, datetime]] = []
    repositories = [str(value) for value in config.github.get("watched_repositories", [])]
    release_limit = int(config.github.get("release_limit_per_repository", 5))
    include_prereleases = bool(config.github.get("include_prereleases", False))
    cutoff = now - timedelta(days=config.lookback_days)

    for full_name in repositories:
        try:
            repo_payload = client.get(f"{API}/repos/{full_name}")
            cache_states.append((repo_payload.cache_state, repo_payload.fetched_at))
            repo = repo_payload.json()
            items.append(apply_provenance(_repo_item(repo, provider="github_watched", item_type="github_repository_snapshot", now=now), repo_payload))
        except Exception as exc:
            failures.append(f"{full_name} metadata: {compact_error(exc)}")
            repo = {"html_url": f"https://github.com/{full_name}"}

        try:
            releases_payload = client.get(
                f"{API}/repos/{full_name}/releases",
                params={"per_page": release_limit},
            )
            cache_states.append((releases_payload.cache_state, releases_payload.fetched_at))
            for release in releases_payload.json():
                if release.get("draft") or (release.get("prerelease") and not include_prereleases):
                    continue
                published = parse_datetime(release.get("published_at"))
                if published and published < cutoff:
                    continue
                tag = str(release.get("tag_name") or release.get("id"))
                release_name = clean_text(release.get("name") or tag, limit=240)
                if full_name.lower() not in release_name.lower():
                    release_name = f"{full_name} {release_name}"
                items.append(
                    apply_provenance(capture_document(SourceItem(
                        provider="github_watched",
                        external_id=f"{full_name.lower()}@{tag}",
                        source_family="github",
                        item_type="github_release",
                        title=clean_text(release_name, limit=300),
                        summary=clean_text(release.get("body")),
                        canonical_url=normalize_url(str(release.get("html_url") or repo["html_url"])),
                        published_at=published,
                        updated_at=parse_datetime(release.get("updated_at")),
                        observed_at=now,
                        entity=full_name.split("/", 1)[0],
                        authority="official",
                        metrics={"repo_full_name": full_name, "release_tag": tag, "release_id": release.get("id")},
                        related_links=[normalize_url(str(repo["html_url"]))],
                    ), str(release.get("body") or ""), "markdown"), releases_payload)
                )
        except Exception as exc:
            failures.append(f"{full_name} releases: {compact_error(exc)}")

    states = [state for state, _ in cache_states]
    return ProviderResult(
        provider="github_watched",
        status=combined_status(item_count=len(items), failures=len(failures), cache_states=states),
        items=items,
        fetched_at=now,
        stale_as_of=oldest_stale_at(cache_states),
        error="; ".join(failures) or None,
        request_count=client.request_count,
    )


def collect_exploratory(config: AppConfig, client: CachedHttpClient, now: datetime) -> ProviderResult:
    items_by_repo: dict[str, SourceItem] = {}
    failures: list[str] = []
    cache_states: list[tuple[str, datetime]] = []
    since = (now - timedelta(days=config.lookback_days)).date().isoformat()
    per_query = min(100, int(config.github.get("exploration_per_query", 15)))
    established_queries = config.github.get("established_queries", [])
    tracking = RepositoryDiscovery(client.database) if established_queries else None
    watched = {str(repo).lower() for repo in config.github.get("watched_repositories", [])}

    def established_item(repo, payload):
        item = apply_provenance(_repo_item(repo, provider="github_explore", item_type="github_repository_snapshot", now=now), payload)
        item.metrics["discovery_origin"] = "established_repository_search"
        # Admit by relevance/activity, but only measured growth can become a fresh event.
        if (item.external_id in watched or repo.get("archived") or repo.get("fork") or repo.get("private")
                or not item.published_at or item.published_at >= now - timedelta(days=config.lookback_days)
                or item.metrics["stars"] < config.ranking.eligibility.github_explore_min_stars
                or not is_relevant(item, config)):
            return None
        return item

    # A separate request budget follows established discoveries even when search stops returning them.
    if tracking:
        for full_name in tracking.prepare(config, now):
            try:
                payload = client.get(f"{API}/repos/{full_name}")
                cache_states.append((payload.cache_state, payload.fetched_at))
                item = established_item(payload.json(), payload)
                if item is not None and item.external_id == full_name:
                    items_by_repo[item.external_id] = item
                elif payload.cache_state in {"live", "validated-cache"}:
                    tracking.remove(full_name)
            except Exception as exc:
                failures.append(f"follow-up {full_name}: {compact_error(exc)}")

    for raw_query in config.github.get("exploration_queries", []):
        query = str(raw_query).replace("{since}", since)
        try:
            payload = client.get(
                f"{API}/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": per_query},
            )
            cache_states.append((payload.cache_state, payload.fetched_at))
            body = payload.json()
            if body.get("incomplete_results"):
                failures.append(f"query {query!r}: GitHub returned incomplete results")
            for repo in body.get("items", []):
                item = apply_provenance(_repo_item(repo, provider="github_explore", item_type="github_exploratory_repository", now=now), payload)
                item.metrics["discovery_query"] = query
                items_by_repo.setdefault(item.external_id, item)
        except Exception as exc:
            failures.append(f"query {query!r}: {compact_error(exc)}")

    for raw_query in established_queries:
        query = str(raw_query).replace("{since}", since)
        try:
            payload = client.get(f"{API}/search/repositories", params={
                "q": query, "sort": "updated", "order": "desc",
                "per_page": config.github.get("established_per_query", 5),
            })
            cache_states.append((payload.cache_state, payload.fetched_at))
            body = payload.json()
            if body.get("incomplete_results"):
                failures.append(f"query {query!r}: GitHub returned incomplete results")
            # Stale/cached search hits cannot admit a repository or refresh its measurement.
            if payload.cache_state not in {"live", "validated-cache"}:
                continue
            for repo in body.get("items", [])[:config.github.get("established_per_query", 5)]:
                item = established_item(repo, payload)
                pushed = parse_datetime(repo.get("pushed_at"))
                if item is None or not pushed or pushed < now - timedelta(days=config.lookback_days):
                    continue
                if item.external_id in items_by_repo and items_by_repo[item.external_id].item_type == "github_repository_snapshot":
                    continue
                if tracking.admit(item.external_id, config, now):
                    item.metrics["discovery_query"] = query
                    items_by_repo[item.external_id] = item
        except Exception as exc:
            failures.append(f"established query {query!r}: {compact_error(exc)}")

    states = [state for state, _ in cache_states]
    return ProviderResult(
        provider="github_explore",
        status=combined_status(item_count=len(items_by_repo), failures=len(failures), cache_states=states),
        items=list(items_by_repo.values()),
        fetched_at=now,
        stale_as_of=oldest_stale_at(cache_states),
        error="; ".join(failures) or None,
        request_count=client.request_count,
        details={"established_tracking": {"active": tracking.count(),
                 "sampled": sum(i.metrics.get("discovery_origin") == "established_repository_search" for i in items_by_repo.values()),
                 "limit": config.github.get("established_tracking_limit", 20),
                 "basis": "recent activity admits observation; only measured star growth creates an event"}} if tracking else {},
    )
