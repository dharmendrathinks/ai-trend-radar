"""Optional Slack delivery with a local outbox, separate from review state."""
from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import json
import logging
import math
import re
import time

import httpx

from ai_trend_radar.db import Database
from ai_trend_radar.developments import sort_key


CARD_LIMIT = 10
CHANGE_LABELS = {"new": "New", "updated": "Updated", "due": "Reminder"}
SCORE_LABELS = {"developer_impact": "Impact", "developer_relevance": "Relevance", "urgency": "Urgency"}


def validate_webhook(value: str | None) -> str:
    value = (value or "").strip()
    if not re.fullmatch(r"https://hooks\.slack\.com/services/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+", value):
        raise ValueError("Set SLACK_WEBHOOK_URL to the real incoming webhook URL in your local .env")
    return value


def _short(value: Any, limit: int) -> str:
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= limit:
        return value
    excerpt = value[:limit - 1]
    sentences = list(re.finditer(r"[.!?](?=\s|$)", excerpt))
    if sentences:
        return excerpt[:sentences[-1].end()]
    if " " in excerpt and not value[limit - 1].isspace():
        excerpt = excerpt.rsplit(" ", 1)[0]
    return excerpt.rstrip() + "…"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plain(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "plain_text", "text": text}}


def _markdown(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text, "verbatim": True}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "plain_text", "text": text}]}


def _link(value: str, label: str) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        url = urlsplit(value)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            return None
    except ValueError:
        return None
    if len(_escape(value)) > 700 or any(c.isspace() or ord(c) < 32 or c in "<>|" for c in value):
        return None
    return f"<{_escape(value)}|{_escape(label.replace('|', '¦'))}>"


def _shortlisted(item: dict[str, Any]) -> bool:
    c = item["candidate"]
    if c.get("assessment_status") != "assessed" or item.get("disposition") != "main":
        return False
    if c.get("disposition", "main") != "main":
        return False
    priority = c.get("developer_priority", {})
    scores = [priority.get("overall"), *(priority.get("categories", {}).get(k, {}).get("score") for k in SCORE_LABELS)]
    return all(isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s) and 0 <= s <= 100 for s in scores)


def build_payload(brief: dict[str, Any]) -> dict[str, Any]:
    """Present a bounded view of saved changes without altering the local inbox."""
    changes = [(key, item) for key in CHANGE_LABELS for item in brief[key]]
    eligible = sorted(((key, item) for key, item in changes if _shortlisted(item)),
                      key=lambda entry: sort_key(entry[1]["candidate"]))
    selected = eligible[:CARD_LIMIT]
    shown_counts = {key: sum(k == key for k, _ in selected) for key in CHANGE_LABELS}
    reminder_label = "reminder" if shown_counts["due"] == 1 else "reminders"
    counts = f"{shown_counts['new']} new · {shown_counts['updated']} updated · {shown_counts['due']} {reminder_label}"
    title = "AI Trend Radar · Developer shortlist"
    try:
        generated = datetime.fromisoformat(brief["generated_at"].replace("Z", "+00:00"))
        generated = generated if generated.tzinfo else generated.replace(tzinfo=UTC)
        date = generated.astimezone(UTC).strftime("%d %b %Y · %H:%M UTC")
    except (ValueError, TypeError, AttributeError):
        date = _short(brief.get("generated_at"), 60) or "Scan date unavailable"
    meta = f"{date} · {counts}"
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": title}}, _context(meta)]
    fallback = [title, meta]

    gaps = [f"{_short(p['provider'], 60).replace('_', ' ')} ({p['status']})" for p in brief.get("provider_status", [])
            if p["status"] in {"failed", "partial", "stale"}]
    coverage = []
    if gaps:
        coverage.append("Collection gaps: " + _short(", ".join(gaps), 1000) + ". Coverage is incomplete.")
    assessment = brief.get("assessment_status")
    assessment_note = {
        "disabled": "LLM assessment is disabled.",
        "partial": "LLM assessment is incomplete; some collected topics were not assessed.",
        "failed": "LLM assessment is unavailable.",
    }.get(assessment)
    if assessment_note:
        coverage.append(assessment_note)
    elif assessment not in {"complete", "ok"}:
        coverage.append("LLM assessment coverage is unavailable in this saved brief.")
    if coverage:
        blocks.append(_context(" ".join(coverage)))
        fallback.extend(coverage)

    if not selected:
        empty = "No new shortlisted developer updates in this saved brief."
        if not changes:
            empty = "No new qualifying changes in this saved brief."
        blocks.append(_plain(empty))
        fallback.append(empty)

    for number, (key, item) in enumerate(selected, 1):
        c = item["candidate"]
        heading = _short(c["title"], 180)
        urls = [c.get("source_url"), *c.get("source_links", [])]
        linked = next((link for url in urls if (link := _link(url, heading))), None)
        priority = c["developer_priority"]
        overall = f"{priority['overall']:g}/100"
        breakdown = " · ".join(f"{label} {priority['categories'][name]['score']:g}" for name, label in SCORE_LABELS.items())
        blocks.extend([{"type": "divider"},
                       _markdown(f"*{number}. {linked or _escape(heading)}*\n*{overall}* · {breakdown}")])
        excerpts = [("Changed", _short(c.get("what_changed"), 200)),
                    ("For", _short(c.get("who_should_care"), 140)),
                    ("Practical impact", _short(c.get("practical_difference"), 220))]
        blocks.append(_markdown("\n".join(f"*{label}:* {_escape(text)}" for label, text in excerpts)))
        context = f"{CHANGE_LABELS[key]} · {_short(c.get('evidence_type'), 100)} · Topic {_short(c['topic_id'], 64)}"
        if not item.get("current", True):
            context += " · Not rechecked in this scan."
        blocks.append(_context(context))
        fallback.extend([f"{number}. {heading}", f"{overall} · {breakdown}",
                         *(f"{label}: {text}" for label, text in excerpts), context])

    footer = [f"Showing {len(selected)} of {len(eligible)} shortlisted changes in this saved brief."]
    if len(eligible) > len(selected):
        footer.append(f"{len(eligible) - len(selected)} more shortlisted changes are available locally.")
    excluded = len(changes) - len(eligible)
    if excluded:
        footer.append(f"{excluded} other changes remain in the local brief (unassessed or outside the shortlist).")
    footer.append("Full details and review commands: latest.brief.md in your configured local reports folder. Unchanged topics are omitted.")
    footer.append(f"Scan: {_short(brief['scan_id'], 64)}")
    blocks.append(_context(" ".join(footer)))
    fallback.extend(footer)
    if selected:
        note = ("All scores are editorial judgments out of 100. Overall = 50% impact + 30% relevance + 20% urgency. "
                "Urgency reflects circumstances at assessment time. Source claims are not independently tested.")
        blocks.append(_context(note))
        fallback.append(note)
    text = _escape("\n".join(fallback))
    if len(text) > 39000:
        # Pathological entity expansion must not truncate the message at Slack.
        # Cut at a complete line so escaped entities and source links stay intact.
        text = text[:39000].rsplit("\n", 1)[0] + "\nAdditional details are available in the local brief."
    return {"text": text, "mrkdwn": False, "parse": "none", "blocks": blocks,
            "unfurl_links": False, "unfurl_media": False}


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
