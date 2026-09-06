"""Optional Slack delivery with a local outbox, separate from review state."""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import json
import logging
import re
import time

import httpx

from youtube_trend_radar.db import Database


def validate_webhook(value: str | None) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"https://hooks\.slack\.com/services/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", value):
        raise ValueError("Set SLACK_WEBHOOK_URL to the real incoming webhook URL in your local .env")
    return value


def _short(value: Any, limit: int) -> str:
    value = str(value or "")
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _plain(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "plain_text", "text": text}}


def _link(value: str) -> str | None:
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return None
    except ValueError:
        return None
    if len(value) > 700 or any(c.isspace() or c in "<>|" for c in value):
        return None
    return "<" + value.replace("&", "&amp;") + "|Source>"


def build_payload(brief: dict[str, Any]) -> dict[str, Any]:
    counts = ", ".join(f"{len(brief[key])} {label}" for key, label in
                       (("new", "new"), ("updated", "updated"), ("due", "due")))
    summary = f"YouTube Trend Radar — {counts}"
    blocks = [_plain(f"{summary}\nGenerated: {brief['generated_at']}\nScan: {brief['scan_id']}")]
    gaps = [f"{_short(p['provider'], 60)} ({p['status']})" for p in brief.get("provider_status", [])
            if p["status"] in {"failed", "partial", "stale"}]
    if gaps:
        blocks.append(_plain("Collection gaps: " + _short(", ".join(gaps), 1500) + ". Coverage is incomplete."))
    shown = 0
    for key, label, limit in (("new", "New discovery", 14), ("updated", "Material update", 3), ("due", "Reminder due", 3)):
        for item in brief[key][:limit]:
            c = item["candidate"]
            lines = [f"{label}: {_short(c.get('display_title') or c['title'], 250)}",
                     f"Event: {_short(item['event_id'], 64)} · {_short(item['reason'], 120)}",
                     f"Event time: {_short(c['event_time'], 40)} ({_short(c.get('event_time_basis', 'source timestamp'), 80)})",
                     f"Evidence: {_short(c['evidence_level'], 120)} · Interest: {_short(c['interest_band'], 60)}"]
            if c.get("why_investigate"):
                lines.append(_short(c["why_investigate"], 350))
            if not item.get("current", True):
                lines.append("Not rechecked in this scan; inspect the source before acting.")
            if item.get("disposition") != "main":
                lines.append("Reminder only; currently outside the main list.")
            blocks.append(_plain("\n".join(lines)))
            links = [link for value in c.get("source_links", [])[:2] if (link := _link(value))]
            if links:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": " · ".join(links), "verbatim": True}})
            shown += 1
    total = sum(len(brief[key]) for key in ("new", "updated", "due"))
    footer = "Unchanged items are omitted. Full brief and watch lists are in your local reports folder."
    if not total:
        footer = "No new qualifying changes in this scan. " + footer
    if shown < total:
        footer = f"Showing {shown} of {total} changes; remaining cards are in the local brief. " + footer
    footer += "\nReview locally: youtube-trend-radar decide EVENT_ID reviewed"
    blocks.append(_plain(footer))
    return {"text": summary, "blocks": blocks, "unfurl_links": False, "unfurl_media": False}


class SlackDelivery:
    def __init__(self, radar_database: Path, webhook: str):
        self.webhook = validate_webhook(webhook)
        self.destination = sha256(self.webhook.encode()).hexdigest()
        # Keep network delivery locks independent of collection/review transactions.
        self.database = Database(radar_database.with_name(radar_database.stem + ".slack.sqlite3"))
        with self.database.connect() as cx:
            cx.execute("""CREATE TABLE IF NOT EXISTS deliveries (
                destination TEXT NOT NULL, scan_id TEXT NOT NULL, payload_json TEXT,
                created_at TEXT NOT NULL, sent_at TEXT, retry_at REAL NOT NULL DEFAULT 0,
                last_error TEXT, PRIMARY KEY (destination, scan_id)
            )""")

    def enqueue(self, brief: dict[str, Any]) -> None:
        if not brief.get("scan_id"):
            raise ValueError("No completed scan brief is available to send")
        payload = build_payload(brief)
        with self.database.connect() as cx:
            cx.execute("""INSERT OR IGNORE INTO deliveries
                (destination, scan_id, payload_json, created_at) VALUES (?, ?, ?, ?)""",
                (self.destination, brief["scan_id"], json.dumps(payload, ensure_ascii=False), brief["generated_at"]))

    def send_pending(self) -> tuple[int, int]:
        """Send at most ten queued briefs. Return confirmed sends and remaining briefs.

        A transaction serializes senders. A crash after Slack accepts a message but
        before commit can still duplicate it: webhooks have no exactly-once receipt.
        """
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        sent = 0
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            for _ in range(10):
                failed = False
                with self.database.connect() as cx:
                    cx.execute("BEGIN IMMEDIATE")
                    row = cx.execute("""SELECT * FROM deliveries WHERE destination=? AND sent_at IS NULL
                        ORDER BY created_at, scan_id LIMIT 1""", (self.destination,)).fetchone()
                    if row is None or row["retry_at"] > time.time():
                        break
                    error, retry_at = self._post(client, json.loads(row["payload_json"]))
                    if error:
                        cx.execute("UPDATE deliveries SET last_error=?, retry_at=? WHERE destination=? AND scan_id=?",
                                   (error, retry_at, self.destination, row["scan_id"]))
                        logging.getLogger(__name__).warning("Slack delivery pending: %s", error)
                        failed = True
                    else:
                        cx.execute("""UPDATE deliveries SET sent_at=?, payload_json=NULL, last_error=NULL
                            WHERE destination=? AND scan_id=?""",
                            (datetime.now(UTC).isoformat(), self.destination, row["scan_id"]))
                        sent += 1
                if failed:
                    break
                time.sleep(1)  # Incoming webhook rate: one message per second.
        with self.database.connect() as cx:
            pending = cx.execute("SELECT COUNT(*) FROM deliveries WHERE destination=? AND sent_at IS NULL",
                                 (self.destination,)).fetchone()[0]
        return sent, pending

    def _post(self, client: httpx.Client, payload: dict[str, Any]) -> tuple[str | None, float]:
        for attempt in range(3):
            try:
                response = client.post(self.webhook, json=payload)
            except httpx.RequestError:
                # Exception text can contain the secret URL. Never log it.
                error = "network error (delivery may be unconfirmed)"
            else:
                if response.status_code == 200 and response.text.strip() == "ok":
                    return None, 0
                error = f"HTTP {response.status_code}; check the webhook and channel settings"
                if response.status_code == 429:
                    raw = response.headers.get("Retry-After", "60")
                    delay = int(raw) if raw.isdigit() and len(raw) <= 8 else 60
                    return "rate limited; retry after Slack's requested delay", time.time() + max(1, delay)
                if response.status_code < 500:
                    return error, 0
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))
        return error, 0
