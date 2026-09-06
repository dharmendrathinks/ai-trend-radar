from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
import json

import httpx
import pytest
import respx

from youtube_trend_radar import pipeline
from youtube_trend_radar.cli import main
from youtube_trend_radar.models import ProviderResult, SourceItem
from youtube_trend_radar.slack import SlackDelivery, build_payload, validate_webhook


WEBHOOK = "https://hooks.slack.com/services/TTEST/BTEST/test-secret"


@pytest.fixture
def brief():
    return {
        "scan_id": "scan-one", "generated_at": "2026-09-06T08:00:00Z",
        "provider_status": [{"provider": "github_watched", "status": "partial", "error": "private details"}],
        "new": [{"event_id": "1234abcd", "revision": 1, "reason": "new discovery", "current": True,
                 "disposition": "main", "candidate": {
                     "title": "New AI tool <!channel>", "event_time": "2026-09-06T07:00:00Z",
                     "evidence_level": "official", "interest_band": "early/limited",
                     "why_investigate": "A concrete new workflow",
                     "source_links": ["https://example.com/release?a=1&b=2"],
                     "youtube_evidence": {"videos": ["must not forward"]}}}],
        "updated": [], "due": [],
    }


@pytest.fixture(autouse=True)
def slack_test_environment(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr("youtube_trend_radar.slack.time.sleep", lambda _: None)


def test_payload_keeps_grounding_warnings_and_limits_without_mentions(brief):
    brief["new"] *= 30
    due = deepcopy(brief["new"][0])
    due.update(current=False, disposition="watch")
    brief["due"] = [due]
    payload = build_payload(brief)
    serialized = json.dumps(payload)
    assert "Showing 15 of 31" in serialized
    assert "Not rechecked" in serialized and "Reminder only" in serialized
    assert "Collection gaps" in serialized and "private details" not in serialized
    assert "must not forward" not in serialized
    assert len(payload["blocks"]) <= 50
    assert all(len(b["text"]["text"]) <= 3000 for b in payload["blocks"])
    for block in payload["blocks"]:
        if "<!channel>" in block["text"]["text"]:
            assert block["text"]["type"] == "plain_text"
    assert "<https://example.com/release?a=1&amp;b=2|Source>" in serialized


def test_no_changes_is_explicit_and_unsafe_links_are_not_rendered(brief):
    brief["new"][0]["candidate"]["source_links"] = ["javascript:alert(1)", "https://example.com/|!channel"]
    assert not any(b["text"]["type"] == "mrkdwn" for b in build_payload(brief)["blocks"])
    brief["new"] = []
    assert "No new qualifying changes" in json.dumps(build_payload(brief))


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
def test_rate_limit_is_persisted_and_respected_on_restart(config, brief, monkeypatch):
    monkeypatch.setattr("youtube_trend_radar.slack.time.time", lambda: 1000)
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(429, headers={"Retry-After": "120"}))
    delivery = SlackDelivery(config.database_path, WEBHOOK)
    delivery.enqueue(brief)
    assert delivery.send_pending() == (0, 1)
    assert SlackDelivery(config.database_path, WEBHOOK).send_pending() == (0, 1)
    assert route.call_count == 1
    monkeypatch.setattr("youtube_trend_radar.slack.time.time", lambda: 1121)
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
    from youtube_trend_radar.db import Database
    from youtube_trend_radar.state import RadarState
    config_path = _mock_scan(tmp_path, monkeypatch)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(SlackDelivery, "enqueue", fail)
    assert main(["scan", "--slack", "--config", str(config_path), "--no-youtube"]) == 1
    state = RadarState(Database(tmp_path / "data/radar.sqlite3"))
    assert len(state.brief(datetime.now(UTC), 10)["new"]) == 1
