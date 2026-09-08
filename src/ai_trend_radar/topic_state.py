"""Development identities layered over the existing occurrence/review ledger."""
from __future__ import annotations

from datetime import datetime
import json
import re

from ai_trend_radar.llm_adapter import digest
from ai_trend_radar.state import RadarState

TOPIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS development_topics (
    topic_id TEXT PRIMARY KEY REFERENCES radar_events(event_id),
    parent_event_id TEXT NOT NULL REFERENCES radar_events(event_id),
    source_anchor TEXT NOT NULL, inherited_state TEXT,
    UNIQUE(parent_event_id, source_anchor)
);
CREATE TABLE IF NOT EXISTS legacy_topic_baselines (
    event_id TEXT PRIMARY KEY REFERENCES radar_events(event_id),
    source_digest TEXT NOT NULL, decision TEXT NOT NULL,
    deferred_until TEXT, converted INTEGER NOT NULL DEFAULT 0
);
"""


def source_digest(signals):
    primary = [s for s in signals if s.get("evidence_role", "event") == "event"]
    primary = [s for s in primary if s.get("authority") == "official"] or primary[:1]
    return digest(sorted((s.get("provider", ""), s.get("external_id", ""), re.sub(r"\s+", " ", s.get("full_text") if s.get("full_text") is not None else s.get("summary", "")).strip(), s.get("title", "")) for s in primary))


def seed_baselines(cx):
    for row in cx.execute("SELECT * FROM radar_events WHERE revision>0 AND event_id NOT IN (SELECT topic_id FROM development_topics)").fetchall():
        payload = json.loads(row["payload_json"])
        current = row["decision"] == "deferred" or row["decision_revision"] >= row["revision"]
        decision = row["decision"] if current and row["decision"] in {"reviewed", "deferred"} else "open"
        cx.execute("INSERT OR IGNORE INTO legacy_topic_baselines(event_id,source_digest,decision,deferred_until) VALUES (?,?,?,?)",
                   (row["event_id"], source_digest(payload.get("observed_signals", [])), decision, row["deferred_until"]))


class TopicState(RadarState):
    def save_topics(self, topics, scan_id):
        parents = {t["parent_event_ids"][0] for t in topics}
        with self.database.connect() as cx:
            cx.execute("BEGIN IMMEDIATE")
            for topic in topics:
                key, parent = topic["topic_id"], topic["parent_event_ids"][0]
                old = cx.execute("SELECT * FROM radar_events WHERE event_id=?", (key,)).fetchone()
                # Model-selected quote length/phrasing is not material evidence.
                previous = json.loads(old['payload_json']) if old else {}
                documents = {**previous.get('document_hashes', {}), **topic.get('document_hashes', {})}
                topic['document_hashes'] = documents
                evidence_hash = digest([topic["source_anchor"], source_digest(topic["observed_signals"]), documents])
                baseline = cx.execute("SELECT * FROM legacy_topic_baselines WHERE event_id=?", (parent,)).fetchone()
                inherited = None
                decision, until = "open", None
                if old is None and baseline and not baseline["converted"] and baseline["source_digest"] == source_digest(topic["observed_signals"]):
                    decision, until = baseline["decision"], baseline["deferred_until"]
                    if decision in {"reviewed", "deferred"}:
                        inherited = f"{decision} inherited from legacy event {parent}"
                if old is None:
                    cx.execute("INSERT INTO radar_events(event_id,decision,deferred_until,decision_revision) VALUES (?,?,?,?)", (key, decision, until, int(decision == "reviewed")))
                    cx.execute("INSERT INTO development_topics VALUES (?,?,?,?)", (key, parent, topic["source_anchor"], inherited))
                    revision, reason = 1, "new development"
                else:
                    revision = old["revision"] + int(old["content_hash"] != evidence_hash)
                    reason = "source evidence changed" if revision != old["revision"] else old["change_reason"]
                topic["revision"] = revision
                row = cx.execute("SELECT e.decision,e.deferred_until,t.inherited_state FROM radar_events e JOIN development_topics t ON e.event_id=t.topic_id WHERE e.event_id=?", (key,)).fetchone()
                topic["review_state"] = dict(row)
                cx.execute("UPDATE radar_events SET payload_json=?,revision=?,content_hash=?,interest_band=?,disposition=?,scan_id=?,change_reason=? WHERE event_id=?",
                           (json.dumps(topic, ensure_ascii=False), revision, evidence_hash, topic["interest_band"], topic["disposition"], scan_id, reason, key))
            # A placeholder must not survive as a second pending copy of its source.
            for parent in parents:
                real = [t for t in topics if t["parent_event_ids"][0] == parent and not t["is_placeholder"]]
                if real:
                    cx.execute("UPDATE radar_events SET disposition='superseded' WHERE event_id IN (SELECT topic_id FROM development_topics WHERE parent_event_id=? AND source_anchor='placeholder')", (parent,))
                    cx.execute("UPDATE legacy_topic_baselines SET converted=1 WHERE event_id=?", (parent,))

    def resolve_topic(self, prefix):
        if not re.fullmatch(r"[0-9a-f]{4,64}", prefix):
            raise ValueError("use a topic ID or an unambiguous prefix of at least four characters")
        with self.database.connect() as cx:
            rows = cx.execute("SELECT topic_id FROM development_topics WHERE topic_id LIKE ?", (prefix + "%",)).fetchall()
        if len(rows) != 1:
            raise ValueError("topic ID is unknown or ambiguous; legacy event operations require --event")
        return rows[0][0]

    def brief(self, now: datetime, top: int, scan_id=None):
        result = super().brief(now, top, scan_id)
        result["schema_version"] = "3.0"
        return result
