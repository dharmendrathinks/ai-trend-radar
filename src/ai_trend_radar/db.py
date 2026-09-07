from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
import json
import sqlite3

from ai_trend_radar.models import ProviderResult, SourceItem, isoformat


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_items (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source_family TEXT NOT NULL,
    item_type TEXT NOT NULL,
    title TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    published_at TEXT,
    updated_at TEXT,
    first_observed_at TEXT NOT NULL,
    last_observed_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (provider, external_id)
);

CREATE TABLE IF NOT EXISTS observations (
    provider TEXT NOT NULL,
    external_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    PRIMARY KEY (provider, external_id, observed_at)
);

CREATE INDEX IF NOT EXISTS idx_observations_item
ON observations(provider, external_id, observed_at);

CREATE TABLE IF NOT EXISTS http_cache (
    cache_key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    body BLOB NOT NULL,
    status_code INTEGER NOT NULL,
    headers_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    etag TEXT,
    last_modified TEXT
);

CREATE TABLE IF NOT EXISTS scans (
    scan_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL,
    config_fingerprint TEXT NOT NULL,
    scoring_version TEXT NOT NULL,
    provider_status_json TEXT NOT NULL,
    report_json TEXT NOT NULL
);
"""


@dataclass(slots=True)
class CacheRecord:
    cache_key: str
    url: str
    body: bytes
    status_code: int
    headers: dict[str, str]
    fetched_at: datetime
    expires_at: datetime
    etag: str | None
    last_modified: str | None
    confirmed_at: datetime | None = None


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


class Database:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise RuntimeError(f"unsupported database schema {version}")
            connection.executescript(SCHEMA)
            if "confirmed_at" not in {r[1] for r in connection.execute("PRAGMA table_info(http_cache)")}:
                connection.execute("ALTER TABLE http_cache ADD COLUMN confirmed_at TEXT")
            if "measurement_kind" not in {r[1] for r in connection.execute("PRAGMA table_info(observations)")}:
                connection.execute("ALTER TABLE observations ADD COLUMN measurement_kind TEXT NOT NULL DEFAULT 'legacy'")
            from ai_trend_radar.state import STATE_SCHEMA
            connection.executescript(STATE_SCHEMA)
            from ai_trend_radar.feedback import FEEDBACK_SCHEMA
            from ai_trend_radar.discovery import DISCOVERY_SCHEMA
            connection.executescript(FEEDBACK_SCHEMA + DISCOVERY_SCHEMA)
            connection.execute("PRAGMA user_version=2")

    def healthcheck(self) -> None:
        with self.connect() as connection:
            connection.execute("SELECT 1").fetchone()

    def get_cache(self, cache_key: str) -> CacheRecord | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM http_cache WHERE cache_key = ?", (cache_key,)).fetchone()
        if row is None:
            return None
        return CacheRecord(
            cache_key=row["cache_key"],
            url=row["url"],
            body=bytes(row["body"]),
            status_code=int(row["status_code"]),
            headers=json.loads(row["headers_json"]),
            fetched_at=_parse_time(row["fetched_at"]),
            expires_at=_parse_time(row["expires_at"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            confirmed_at=_parse_time(row["confirmed_at"]) if row["confirmed_at"] else None,
        )

    def put_cache(self, record: CacheRecord) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO http_cache
                    (cache_key, url, body, status_code, headers_json, fetched_at, expires_at, etag, last_modified, confirmed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    url=excluded.url, body=excluded.body, status_code=excluded.status_code,
                    headers_json=excluded.headers_json, fetched_at=excluded.fetched_at,
                    expires_at=excluded.expires_at, etag=excluded.etag,
                    last_modified=excluded.last_modified, confirmed_at=excluded.confirmed_at
                """,
                (
                    record.cache_key,
                    record.url,
                    record.body,
                    record.status_code,
                    json.dumps(record.headers, sort_keys=True),
                    isoformat(record.fetched_at),
                    isoformat(record.expires_at),
                    record.etag,
                    record.last_modified,
                    isoformat(record.confirmed_at),
                ),
            )

    def add_observed_growth(self, item: SourceItem) -> None:
        numeric = {
            key: value
            for key, value in item.metrics.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        if not numeric:
            return
        with self.connect() as connection:
            row = connection.execute(
                """SELECT observed_at, metrics_json FROM observations
                   WHERE provider = ? AND external_id = ? AND measurement_kind != 'legacy'
                   ORDER BY observed_at ASC LIMIT 1""",
                (item.provider, item.external_id),
            ).fetchone()
        growth: dict[str, Any] = {
            "available": row is not None,
            "first_observed_at": isoformat(item.measurement_time),
            "observation_duration_hours": 0.0,
            "metrics": {},
        }
        first_metrics = numeric
        if row is not None:
            first_time = _parse_time(row["observed_at"])
            first_metrics = json.loads(row["metrics_json"])
            growth["first_observed_at"] = isoformat(first_time)
            growth["observation_duration_hours"] = round(
                max(0.0, (item.measurement_time - first_time).total_seconds() / 3600), 3
            )
        for key, current in numeric.items():
            initial = first_metrics.get(key, current)
            growth["metrics"][key] = {
                "initial": initial,
                "current": current,
                "delta": current - initial,
            }
        item.metrics["observed_growth"] = growth

    def record_provider_result(self, result: ProviderResult) -> None:
        with self.connect() as connection:
            for item in result.items:
                first = connection.execute("SELECT first_observed_at FROM source_items WHERE provider=? AND external_id=?", (item.provider, item.external_id)).fetchone()
                item.first_seen_at = _parse_time(first[0]) if first else item.observed_at
                payload = json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=False)
                connection.execute(
                    """
                    INSERT INTO source_items
                        (provider, external_id, source_family, item_type, title, canonical_url,
                         published_at, updated_at, first_observed_at, last_observed_at, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, external_id) DO UPDATE SET
                        source_family=excluded.source_family, item_type=excluded.item_type,
                        title=excluded.title, canonical_url=excluded.canonical_url,
                        published_at=excluded.published_at, updated_at=excluded.updated_at,
                        last_observed_at=excluded.last_observed_at, payload_json=excluded.payload_json
                    """,
                    (
                        item.provider,
                        item.external_id,
                        item.source_family,
                        item.item_type,
                        item.title,
                        item.canonical_url,
                        isoformat(item.published_at),
                        isoformat(item.updated_at),
                        isoformat(item.observed_at),
                        isoformat(item.observed_at),
                        payload,
                    ),
                )
                numeric = {
                    key: value
                    for key, value in item.metrics.items()
                    if isinstance(value, (int, float)) and not isinstance(value, bool)
                }
                if numeric and item.cache_state in {"live", "validated-cache"}:
                    connection.execute(
                        """INSERT OR IGNORE INTO observations
                           (provider, external_id, observed_at, metrics_json, measurement_kind)
                           VALUES (?, ?, ?, ?, ?)""",
                        (item.provider, item.external_id, isoformat(item.measurement_time), json.dumps(numeric, sort_keys=True), item.cache_state),
                    )

    def record_scan(
        self,
        *,
        scan_id: str,
        started_at: datetime,
        completed_at: datetime,
        status: str,
        config_fingerprint: str,
        scoring_version: str,
        provider_statuses: list[dict[str, Any]],
        report: dict[str, Any],
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO scans
                   (scan_id, started_at, completed_at, status, config_fingerprint,
                    scoring_version, provider_status_json, report_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    scan_id,
                    isoformat(started_at),
                    isoformat(completed_at),
                    status,
                    config_fingerprint,
                    scoring_version,
                    json.dumps(provider_statuses, sort_keys=True),
                    json.dumps(report, sort_keys=True, ensure_ascii=False),
                ),
            )
