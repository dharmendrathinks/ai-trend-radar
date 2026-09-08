from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import json

import httpx
import pytest
import respx

from ai_trend_radar import pipeline
from ai_trend_radar.cli import main
from ai_trend_radar.models import ProviderResult, SourceItem
from ai_trend_radar.slack import SlackDelivery, build_payload, validate_webhook


WEBHOOK = "https://hooks.slack.com/services/TTEST/BTEST/test-secret"


@pytest.fixture
def brief():
    return {
        "schema_version": "3.0", "assessment_status": "ok",
        "scan_id": "scan-one", "generated_at": "2026-09-06T08:00:00Z",
        "provider_status": [{"provider": "github_watched", "status": "partial", "error": "private details"}],
        "new": [{"event_id": "1234abcd", "revision": 1, "reason": "new discovery", "current": True,
                 "disposition": "main", "candidate": {
                     "topic_id": "1234abcd", "assessment_status": "assessed", "disposition": "main",
                     "title": "New AI tool <!channel>", "event_time": "2026-09-06T07:00:00Z",
                     "what_changed": "Adds source-linked traces for agent runs.",
                     "who_should_care": "Developers debugging coding agents.",
                     "practical_difference": "Inspect failed runs before trusting reported completion.",
                     "evidence_type": "publisher statement",
                     "developer_priority": {"overall": 79.0, "categories": {
                         "developer_impact": {"score": 74}, "developer_relevance": {"score": 88},
                         "urgency": {"score": 78}}},
                     "evidence_level": "official", "interest_band": "early/limited",
                     "why_investigate": "A concrete new workflow",
                     "source_links": ["https://example.com/release?a=1&b=2"],
                     "youtube_evidence": {"videos": ["must not forward"]}}}],
        "updated": [], "due": [],
    }


@pytest.fixture(autouse=True)
def slack_test_environment(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr("ai_trend_radar.slack.time.sleep", lambda _: None)


def _texts(payload):
    return [text["text"] for block in payload["blocks"]
            for text in ([block["text"]] if "text" in block else block.get("elements", []))]


def _item(brief, name, overall=79.0):
    item = deepcopy(brief["new"][0])
    item["event_id"] = name
    item["candidate"].update(topic_id=name, title=name)
    item["candidate"]["developer_priority"]["overall"] = overall
    return item


def _headings(payload):
    return [text for text in _texts(payload) if text.startswith("*") and text[1:2].isdigit()]


def test_payload_has_readable_cards_safe_links_and_accessible_fallback(brief):
    brief["assessment_status"] = "partial"
    payload = build_payload(brief)
    visible = "\n".join(_texts(payload))
    serialized = json.dumps(payload)
    assert payload["blocks"][0]["type"] == "header"
    assert "06 Sep 2026 · 08:00 UTC · 1 new · 0 updated · 0 reminders" in visible
    assert "*79/100* · Impact 74 · Relevance 88 · Urgency 78" in visible
    assert all(label in visible for label in ("*Changed:*", "*For:*", "*Practical impact:*", "Topic 1234abcd"))
    assert "Collection gaps: github watched (partial)" in visible
    assert "assessment is incomplete" in visible and "private details" not in serialized
    assert visible.count("not independently tested") == 1
    assert "ai-trend-radar decide" not in visible
    assert "must not forward" not in serialized
    assert "<!channel>" not in serialized
    assert "<https://example.com/release?a=1&amp;b=2|New AI tool &lt;!channel&gt;>" in visible
    assert "79/100" in payload["text"] and "Adds source-linked traces" in payload["text"]
    assert payload["mrkdwn"] is False and payload["parse"] == "none"
    assert payload["unfurl_links"] is payload["unfurl_media"] is False
    assert all(b["text"]["verbatim"] for b in payload["blocks"] if b.get("text", {}).get("type") == "mrkdwn")


def test_top_ten_combines_sections_preserves_precision_and_does_not_mutate_brief(brief):
    brief["new"] = [_item(brief, f"new-{i}", 60 + i) for i in range(12)]
    brief["updated"] = [_item(brief, "updated-best", 78.9)]
    reminder = _item(brief, "reminder-best", 90)
    reminder["current"] = False
    brief["due"] = [reminder]
    original = deepcopy(brief)
    payload = build_payload(brief)
    headings = _headings(payload)
    visible = "\n".join(_texts(payload))
    assert len(headings) == 10
    assert "reminder-best" in headings[0] and "updated-best" in headings[1]
    assert "*78.9/100*" in headings[1]
    assert "new-11" in headings[2] and "new-4" in headings[-1]
    assert "8 new · 1 updated · 1 reminder" in visible
    assert "Showing 10 of 14 shortlisted changes" in visible
    assert "4 more shortlisted changes" in visible
    assert "Reminder · publisher statement" in visible and "Not rechecked in this scan" in visible
    assert "Updated · publisher statement" in visible
    assert brief == original


def test_shortlist_excludes_unassessed_watch_and_legacy_items_without_backfill(brief):
    unassessed = _item(brief, "unassessed")
    unassessed["candidate"]["assessment_status"] = "unassessed"
    unscored = _item(brief, "unscored")
    unscored["candidate"]["developer_priority"]["overall"] = None
    watch = _item(brief, "watch")
    watch["disposition"] = "watch"
    old_watch = _item(brief, "old-watch")
    old_watch["candidate"]["disposition"] = "watch"
    legacy = {"disposition": "main", "candidate": {"title": "legacy-discovery", "discovery_priority": 99}}
    brief["new"].extend([unassessed, unscored, legacy])
    brief["due"] = [watch, old_watch]
    payload = build_payload(brief)
    assert len(_headings(payload)) == 1
    assert "5 other changes remain in the local brief" in "\n".join(_texts(payload))
    assert all(name not in json.dumps(payload) for name in ("legacy-discovery", "old-watch", "unscored"))


@pytest.mark.parametrize("score", [None, True, "79", float("nan"), float("inf"), -1, 101])
def test_invalid_scores_are_not_presented_as_a_shortlist(brief, score):
    brief["new"][0]["candidate"]["developer_priority"]["overall"] = score
    assert not _headings(build_payload(brief))


def test_ties_use_existing_impact_then_relevance_urgency_time_and_topic_order(brief):
    items = [_item(brief, name, 79) for name in ("a", "b", "c", "d", "e", "f")]
    items[0]["candidate"]["developer_priority"]["categories"]["developer_impact"]["score"] = 90
    items[1]["candidate"]["developer_priority"]["categories"]["developer_relevance"]["score"] = 95
    items[2]["candidate"]["developer_priority"]["categories"]["urgency"]["score"] = 90
    items[3]["candidate"]["event_time"] = "2026-09-07T07:00:00Z"
    brief["new"] = list(reversed(items))
    headings = _headings(build_payload(brief))
    assert all(f"|{name}>" in heading for name, heading in zip("abcdef", headings))


@pytest.mark.parametrize("status, expected", [("ok", None), ("disabled", "assessment is disabled"),
    ("partial", "assessment is incomplete"), ("failed", "assessment is unavailable"),
    (None, "coverage is unavailable")])
@pytest.mark.parametrize("has_changes", [True, False])
def test_empty_shortlist_and_assessment_status_are_explicit(brief, status, expected, has_changes):
    brief["assessment_status"] = status
    brief["new"][0]["candidate"]["assessment_status"] = "unassessed"
    if not has_changes:
        brief["new"] = []
    payload = build_payload(brief)
    visible = "\n".join(_texts(payload))
    assert not _headings(payload)
    assert ("No new shortlisted developer updates" if has_changes else "No new qualifying changes") in visible
    if expected:
        assert expected in visible and expected in payload["text"]
    assert "Collection gaps" in visible


def test_primary_link_wins_then_falls_back_to_a_valid_source(brief):
    c = brief["new"][0]["candidate"]
    c["source_url"] = "https://primary.test/release"
    assert "<https://primary.test/release|" in _headings(build_payload(brief))[0]
    c["source_url"] = "javascript:alert(1)"
    assert "<https://example.com/release?" in _headings(build_payload(brief))[0]


@pytest.mark.parametrize("url", ["javascript:alert(1)", "https://example.com/|!channel", "https://user:password@example.com",
    "https://example.com/a b", "https://[invalid", "https://example.com/\x00", "https://example.com/" + "&" * 200])
def test_unsafe_links_render_an_unlinked_title(brief, url):
    c = brief["new"][0]["candidate"]
    c.update(source_url=url, source_links=[url])
    heading = _headings(build_payload(brief))[0]
    assert "*1. New AI tool &lt;!channel&gt;*" in heading
    assert "<https" not in heading


def test_source_text_cannot_inject_mentions_or_link_labels(brief):
    c = brief["new"][0]["candidate"]
    c["title"] = "Tool | <!here> <@U123> & <https://evil.test|click>"
    c["what_changed"] = "<!channel> <#C123> @here & <unsafe>"
    payload = build_payload(brief)
    serialized = json.dumps(payload)
    assert not any(s in serialized for s in ("<!here>", "<@U123>", "<!channel>", "<#C123>", "<https://evil.test"))
    assert "Tool ¦ &lt;!here&gt;" in _headings(payload)[0]
    assert "&amp; &lt;unsafe&gt;" in serialized


def test_excerpts_normalize_whitespace_and_use_sentence_or_word_boundaries(brief):
    c = brief["new"][0]["candidate"]
    sentence = "Adds execution traces for failed agent runs."
    c["what_changed"] = sentence + " " + "Extra detail " * 30
    c["who_should_care"] = "  Developers\n using\t coding agents.  "
    c["practical_difference"] = "Inspect failed runs " * 40
    visible = "\n".join(_texts(build_payload(brief)))
    assert f"*Changed:* {sentence}\n" in visible
    assert "*For:* Developers using coding agents." in visible
    impact = next(line for line in visible.splitlines() if line.startswith("*Practical impact:*"))
    assert impact.endswith("…") and len(impact.removeprefix("*Practical impact:* ")) <= 220


def test_slack_limits_include_encoded_text_contexts_unicode_and_long_tokens(brief):
    c = brief["new"][0]["candidate"]
    for key in ("title", "what_changed", "who_should_care", "practical_difference"):
        c[key] = "&" * 10000
    c["evidence_type"] = "证据 " * 1000
    c["topic_id"] = "x" * 10000
    c["source_url"] = "https://example.com/" + "&" * 200
    brief["new"] *= 10
    payload = build_payload(brief)
    assert len(payload["blocks"]) <= 50
    assert len(_headings(payload)) == 10
    for block in payload["blocks"]:
        if block["type"] == "header":
            assert 1 <= len(block["text"]["text"]) <= 150
        elif block["type"] == "section":
            assert 1 <= len(block["text"]["text"]) <= 3000
        elif block["type"] == "context":
            assert 1 <= len(block["elements"]) <= 10
            assert all(1 <= len(e["text"]) <= 2000 for e in block["elements"])
        else:
            assert block["type"] == "divider"
    assert "证据" in "\n".join(_texts(payload))
    assert len(payload["text"]) <= 40000


@pytest.mark.parametrize("value", [None, "your_webhook_url_here", "https://example.com/services/T/B/S",
                                  WEBHOOK + "?secret=value", "http://hooks.slack.com/services/T/B/S"])
def test_invalid_webhooks_do_not_echo_values(value):
    with pytest.raises(ValueError) as error:
        validate_webhook(value)
    assert "test-secret" not in str(error.value)


@respx.mock
def test_outbox_retries_failure_survives_restart_and_deduplicates(config, brief, caplog):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(503, text=WEBHOOK))
    delivery = SlackDelivery(config.database_path, WEBHOOK)
    delivery.enqueue(brief)
    assert delivery.send_pending() == (0, 1)
    assert route.call_count == 3
    assert WEBHOOK not in caplog.text
    with delivery.database.connect() as cx:
        row = cx.execute("SELECT * FROM deliveries").fetchone()
        assert WEBHOOK not in str(tuple(row))
        assert row["sent_at"] is None
    later = SlackDelivery(config.database_path, WEBHOOK)
    later.enqueue(brief)
    route.mock(return_value=httpx.Response(200, text="ok"))
    assert later.send_pending() == (1, 0)
    assert later.send_pending() == (0, 0)
    assert route.call_count == 4
    with later.database.connect() as cx:
        assert cx.execute("SELECT payload_json FROM deliveries").fetchone()[0] is None


@respx.mock
def test_existing_queued_payload_is_not_reformatted_or_resent(config, brief):
    delivery = SlackDelivery(config.database_path, WEBHOOK)
    delivery.enqueue(brief)
    original = {"text": "An older queued format", "blocks": []}
    with delivery.database.connect() as cx:
        cx.execute("UPDATE deliveries SET payload_json=?", (json.dumps(original),))
    # A newer renderer or changed saved brief must not replace a pending payload.
    brief["new"][0]["candidate"]["title"] = "A changed title"
    delivery.enqueue(brief)
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, text="ok"))
    assert delivery.send_pending() == (1, 0)
    assert json.loads(route.calls[0].request.content) == original
    delivery.enqueue(brief)
    assert delivery.send_pending() == (0, 0) and route.call_count == 1


@respx.mock
def test_rate_limit_is_persisted_and_respected_on_restart(config, brief, monkeypatch):
    monkeypatch.setattr("ai_trend_radar.slack.time.time", lambda: 1000)
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))
    delivery = SlackDelivery(config.database_path, WEBHOOK)
    delivery.enqueue(brief)
    assert delivery.send_pending() == (0, 1)
    assert SlackDelivery(config.database_path, WEBHOOK).send_pending() == (0, 1)
    assert route.call_count == 1
    monkeypatch.setattr("ai_trend_radar.slack.time.time", lambda: 1121)
    route.mock(return_value=httpx.Response(200, text="ok"))
    assert delivery.send_pending() == (1, 0)


@respx.mock
def test_permanent_errors_and_redirects_are_not_retried_or_followed(config, brief):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(302, headers={"Location": "https://example.com"}))
    delivery = SlackDelivery(config.database_path, WEBHOOK)
    delivery.enqueue(brief)
    assert delivery.send_pending() == (0, 1)
    assert route.call_count == 1
    route.mock(return_value=httpx.Response(403, text=WEBHOOK))
    assert delivery.send_pending() == (0, 1)
    assert route.call_count == 2


@respx.mock
def test_timeout_is_redacted_and_does_not_confirm_delivery(config, brief, caplog):
    route = respx.post(WEBHOOK).mock(side_effect=httpx.ReadTimeout(WEBHOOK))
    delivery = SlackDelivery(config.database_path, WEBHOOK)
    delivery.enqueue(brief)
    assert delivery.send_pending() == (0, 1)
    assert route.call_count == 3
    assert WEBHOOK not in caplog.text


@respx.mock
def test_new_destination_does_not_receive_another_channels_pending_briefs(config, brief):
    old = SlackDelivery(config.database_path, WEBHOOK)
    old.enqueue(brief)
    new = SlackDelivery(config.database_path, WEBHOOK + "-new")
    assert new.send_pending() == (0, 0)


def _mock_scan(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    config_path = tmp_path / "config.toml"
    config_path.write_text((root / "config.example.toml").read_text())
    now = datetime.now(UTC)
    item = SourceItem("hacker_news", "98765", "hacker_news", "hacker_news_story",
                      "Show HN: Unknown AI coding tool", "A new developer tool for AI coding",
                      "https://example.test/new-tool", now, None, now, metrics={"points": 37, "comments": 14})
    for module, method, name in ((pipeline.official, "collect", "official"),
                                 (pipeline.github, "collect_watched", "github_watched"),
                                 (pipeline.github, "collect_exploratory", "github_explore"),
                                 (pipeline.huggingface, "collect", "huggingface")):
        monkeypatch.setattr(module, method, lambda *a, p=name: ProviderResult(p, "ok", [], now))
    monkeypatch.setattr(pipeline.hackernews, "collect", lambda *a: ProviderResult("hacker_news", "ok", [item], now))
    monkeypatch.setattr(pipeline.youtube, "validate", lambda *a, **kw: ProviderResult("youtube", "disabled", [], now))
    return config_path


@respx.mock
def test_scans_are_opt_in_and_pending_briefs_survive_later_scan(tmp_path, monkeypatch):
    config_path = _mock_scan(tmp_path, monkeypatch)
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(503))
    assert main(["scan", "--config", str(config_path), "--no-youtube"]) == 0
    assert route.call_count == 0
    assert not (tmp_path / "data/radar.slack.sqlite3").exists()
    # Explicit notify can deliver a brief already saved and acknowledged locally.
    assert main(["notify", "--config", str(config_path)]) == 2
    assert (tmp_path / "reports/latest.brief.md").is_file()
    first_scan_id = json.loads((tmp_path / "reports/latest.brief.json").read_text())["scan_id"]
    assert main(["scan", "--slack", "--config", str(config_path), "--no-youtube"]) == 2
    assert len(list((tmp_path / "reports").glob("scan-*.json"))) == 2
    route.mock(return_value=httpx.Response(200, text="ok"))
    assert main(["notify", "--config", str(config_path)]) == 0
    successful = [call for call in route.calls if call.response.status_code == 200]
    assert len(successful) == 2
    assert first_scan_id in successful[0].request.content.decode()
    count = route.call_count
    assert main(["notify", "--config", str(config_path)]) == 0
    assert route.call_count == count


def test_queue_write_failure_does_not_acknowledge_brief(tmp_path, monkeypatch):
    from ai_trend_radar.db import Database
    from ai_trend_radar.state import RadarState
    config_path = _mock_scan(tmp_path, monkeypatch)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(SlackDelivery, "enqueue", fail)
    assert main(["scan", "--slack", "--config", str(config_path), "--no-youtube"]) == 1
    state = RadarState(Database(tmp_path / "data/radar.sqlite3"))
    assert len(state.brief(datetime.now(UTC), 10)["new"]) == 1
