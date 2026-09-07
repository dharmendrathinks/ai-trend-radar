from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from ai_trend_radar import cli, enrichment, pipeline
from ai_trend_radar import llm_adapter
from ai_trend_radar.llm_scoring import rank_updates
from ai_trend_radar.config import ConfigError, load_config
from ai_trend_radar.models import ProviderResult, SourceItem
from ai_trend_radar.reports import build_report, render_markdown

NOW = datetime.now(UTC)
NOTES = "gemma4 now supports images and audio on MLX engine"


def release(tag="v0.33.3", **overrides):
    item = SourceItem("github_watched", f"ollama/ollama@{tag}", "github", "github_release",
        f"Ollama {tag}", NOTES, f"https://github.com/ollama/ollama/releases/tag/{tag}",
        NOW - timedelta(days=6), None, NOW, entity="Ollama", authority="official", full_text=NOTES,
        metrics={"repo_full_name": "ollama/ollama", "release_tag": tag})
    return replace(item, **overrides)


def valid(evidence, *_):
    return {"status": "valid", "errors": [], "tool_items": [], "turn_completed": True,
        "response": {"angles": [{"title": "Gemma4 image and audio support on MLX",
            "developer_value": "Try image and audio inputs with Gemma4 on MLX.",
            "editorial": {key: {"score": 75, "reason": "A concrete workflow change for the configured audience."}
                          for key in ("developer_impact", "demo_potential", "audience_fit")},
            "evidence_quotes": [evidence["notes"]], "caveats": ["No benchmark is provided."]}],
            "abstain_reason": ""}}


@pytest.fixture
def model(monkeypatch):
    calls = []
    monkeypatch.setattr(enrichment.shutil, "which", lambda _: "/test/codex")
    def extract(evidence, *args):
        calls.append(evidence)
        return valid(evidence)
    monkeypatch.setattr(enrichment.adapter, "model_result", extract)
    return calls


def test_disabled_has_no_model_or_cache_work(config, monkeypatch):
    monkeypatch.setattr(enrichment.shutil, "which", lambda _: pytest.fail("disabled must not resolve Codex"))
    assert enrichment.discover_updates([release()], config, enabled=False)["status"] == "disabled"
    assert not config.database_path.with_suffix(".llm").exists()


def test_cache_reuses_new_observation_but_invalidates_changed_notes_model_and_prompt(config, model, monkeypatch):
    item = release()
    first = enrichment.discover_updates([item, item], config, enabled=True)
    assert first["new_calls"] == 1 and len(first["updates"]) == 1
    again = enrichment.discover_updates([replace(item, observed_at=NOW + timedelta(hours=1))], config, enabled=True)
    assert again["cached_results"] == 1 and again["new_calls"] == 0
    changed = enrichment.discover_updates([replace(item, full_text=NOTES + ".")], config, enabled=True)
    assert changed["new_calls"] == 1
    changed_model = replace(config, llm=replace(config.llm, model="another-model"))
    assert enrichment.discover_updates([item], changed_model, enabled=True)["new_calls"] == 1
    monkeypatch.setattr(enrichment.adapter, "ADAPTER_VERSION", "new-prompt-version")
    assert enrichment.discover_updates([item], config, enabled=True)["new_calls"] == 1
    assert len(model) == 4


def test_failure_cached_abstention_distinct_and_breaker(config, monkeypatch, model):
    calls = []
    def failure(*_):
        calls.append(1)
        raise RuntimeError("secret detail must not be exposed")
    monkeypatch.setattr(enrichment.adapter, "model_result", failure)
    items = [release(str(i)) for i in range(5)]
    result = enrichment.discover_updates(items, config, enabled=True)
    assert (result["new_calls"], result["failures"], result["skipped"], result["abstentions"]) == (3, 3, 2, 0)
    assert "secret detail" not in json.dumps(result)
    # The newest-first tie-break selected URLs 4, 3, 2; use a cached release explicitly.
    cached = enrichment.discover_updates([items[4]], config, enabled=True)
    assert cached["cached_results"] == 1 and cached["failures"] == 1 and cached["new_calls"] == 0
    monkeypatch.setattr(enrichment.adapter, "model_result", lambda *_: {
        "status": "valid", "response": {"angles": [], "abstain_reason": "Maintenance only"}})
    result = enrichment.discover_updates([release("maintenance")], config, enabled=True)
    assert result["abstentions"] == 1 and result["failures"] == 0 and not result["updates"]


def test_call_release_and_input_limits(config, model):
    config.llm.max_calls_per_scan = 1
    result = enrichment.discover_updates([release("1"), release("2")], config, enabled=True)
    assert result["new_calls"] == 1 and result["skipped"] == 1
    config.llm.max_releases = 1
    result = enrichment.discover_updates([release("1"), release("2")], config, enabled=True)
    assert result["cached_results"] == 1 and result["skipped"] == 1
    result = enrichment.discover_updates([release("3", full_text="x" * 20001),
        release("4", content_complete=False), release("5", full_text=None)], config, enabled=True)
    assert result["skipped"] == 3 and result["new_calls"] == 0


@pytest.mark.parametrize("corruption", ["json", "quote", "status"])
def test_corrupt_cache_is_rejected(config, model, corruption):
    result = enrichment.discover_updates([release()], config, enabled=True)
    path = config.database_path.with_suffix(".llm") / (result["updates"][0]["cache_key"] + ".json")
    saved = json.loads(path.read_text())
    if corruption == "json":
        path.write_text("not json")
    else:
        if corruption == "quote":
            saved["result"]["response"]["angles"][0]["evidence_quotes"] = ["Invented quotation"]
        else:
            saved["result"]["status"] = "invented-status"
        path.write_text(json.dumps(saved))
    result = enrichment.discover_updates([release()], config, enabled=True)
    assert result["new_calls"] == 1 and result["cached_results"] == 0 and result["warnings"]


def test_missing_binary_and_unwritable_cache_do_not_break_report(config, model, monkeypatch):
    monkeypatch.setattr(enrichment.shutil, "which", lambda _: None)
    result = enrichment.discover_updates([release()], config, enabled=True)
    assert result["status"] == "unavailable" and not model
    monkeypatch.setattr(enrichment.shutil, "which", lambda _: "/test/codex")
    monkeypatch.setattr(enrichment, "_atomic_write", lambda *_: (_ for _ in ()).throw(OSError("read only")))
    result = enrichment.discover_updates([release()], config, enabled=True)
    assert result["status"] == "partial" and result["updates"] and result["warnings"]


def test_llm_section_first_and_schema_additive(config, model):
    data = enrichment.discover_updates([release()], config, enabled=True)
    first = data["updates"][0]
    first["angles"].append({**first["angles"][0], "title": "Second capability"})
    data["updates"].append({**first, "angles": [{**first["angles"][0], "title": "Another release"}]})
    report = build_report(scan_id="test", started_at=NOW, completed_at=NOW, status="complete",
        config=config, provider_results=[], candidates=[], llm_updates=data)
    markdown = render_markdown(report)
    assert report["schema_version"] == "2.3"
    assert markdown.index("## LLM-discovered updates") < markdown.index("## Provider status") < markdown.index("## Top Opportunities")
    assert NOTES in markdown and "No benchmark" in markdown and "not change Discovery Priority" in markdown
    assert report["recommendations"] == []
    assert "| Developer impact /100 | Demo potential /100 | Freshness /100 | Audience impact /100 | Overall /100 |" in markdown
    detail = markdown.split("### 1.", 1)[1]
    assert detail.index("**Developer impact:") < detail.index("**Demo potential:") < detail.index("**Freshness:") < detail.index("**Audience impact:")
    assert [line for line in markdown.splitlines() if line.startswith("### ")] == [
        "### 1. Another release",
        "### 2. Gemma4 image and audio support on MLX",
        "### 3. Second capability",
    ]


def test_low_ranked_release_survives_as_supplement_only(tmp_path, monkeypatch, model):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "config.toml"
    path.write_text((root / "config.example.toml").read_text())
    item = release()
    monkeypatch.setattr(pipeline.official, "collect", lambda *_: ProviderResult("official", "ok", [], NOW))
    monkeypatch.setattr(pipeline.github, "collect_watched", lambda *_: ProviderResult("github_watched", "ok", [item], NOW))
    monkeypatch.setattr(pipeline.github, "collect_exploratory", lambda *_: ProviderResult("github_explore", "ok", [], NOW))
    monkeypatch.setattr(pipeline.hackernews, "collect", lambda *_: ProviderResult("hacker_news", "ok", [], NOW))
    monkeypatch.setattr(pipeline.huggingface, "collect", lambda *_: ProviderResult("huggingface", "ok", [], NOW))
    monkeypatch.setattr(pipeline.youtube, "validate", lambda *_, **kw: ProviderResult("youtube", "disabled", [], NOW))
    assert pipeline.run_scan(path, no_youtube=True, llm=False) == 0
    normal = json.loads((tmp_path / "reports/latest.json").read_text())
    assert pipeline.run_scan(path, no_youtube=True, llm=True) == 0
    enriched = json.loads((tmp_path / "reports/latest.json").read_text())
    assert not normal["recommendations"] and not enriched["recommendations"]
    assert len(enriched["llm_updates"]["updates"]) == 1
    assert normal["release_watch"][0]["discovery_priority"] == enriched["release_watch"][0]["discovery_priority"]
    brief = json.loads((tmp_path / "reports/latest.brief.json").read_text())
    assert "llm_updates" not in brief


def test_cli_overrides():
    parser = cli.build_parser()
    assert parser.parse_args(["scan"]).llm is None
    assert parser.parse_args(["scan", "--llm"]).llm is True
    assert parser.parse_args(["scan", "--no-llm"]).llm is False
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "--llm", "--no-llm"])


@pytest.mark.parametrize("setting", ['enabled = "yes"', 'max_calls_per_scan = 0',
    'max_releases = 101', 'timeout_seconds = true', 'max_input_chars = 100001',
    'model = ""', 'reasoning_effort = "invalid"', 'codex_binary = 2'])
def test_invalid_llm_config(tmp_path, setting):
    path = tmp_path / "bad.toml"
    path.write_text("[llm]\n" + setting)
    with pytest.raises(ConfigError, match="llm"):
        load_config(path)


def test_existing_config_keeps_llm_disabled(tmp_path):
    path = tmp_path / "old.toml"
    path.write_text("[scan]\nlookback_days = 7\n")
    assert not load_config(path).llm.enabled


def scored_release(title="Example", score=80, published=NOW):
    response = valid({"notes": NOTES})["response"]
    angle = response["angles"][0]
    angle["title"] = title
    for criterion in angle["editorial"].values():
        criterion["score"] = score
    return {"title": "Release " + title, "source_url": "https://github.com/example/" + title,
        "published_at": published.isoformat() if published else None, "angles": [angle]}


def test_weighted_scores_global_ranking_and_cached_time_recalculation():
    weak = scored_release("Weak", 25)
    strong = scored_release("Strong", 90, NOW - timedelta(hours=48))
    # A release's second topic must rank globally, not be pinned beside its first.
    weak["angles"].append(scored_release("Best", 100)["angles"][0])
    data = {"updates": [weak, strong]}
    result = rank_updates(data, NOW, 48, "Test audience")
    assert [t["title"] for t in result["ranked_topics"]] == ["Best", "Strong", "Weak"]
    priority = result["ranked_topics"][1]["video_priority"]
    assert priority["categories"]["freshness"]["score"] == 50
    assert priority["overall"] == 82  # 27 + 27 + 18 + 10
    later = rank_updates(data, NOW + timedelta(hours=48), 48, "Test audience")
    assert later["ranked_topics"][1]["video_priority"]["overall"] == 77
    assert "ranked_topics" not in data and "video_priority" not in strong["angles"][0]
    assert result["ranking"]["audience"] == "Test audience"


@pytest.mark.parametrize("date", [None, "invalid", "2026-09-01T00:00:00", (NOW + timedelta(days=1)).isoformat()])
def test_unknown_or_future_dates_are_not_fake_freshness(date):
    release = scored_release()
    release["published_at"] = date
    topic = rank_updates({"updates": [release]}, NOW, 48, "Audience")["ranked_topics"][0]
    assert topic["video_priority"]["overall"] is None
    assert topic["video_priority"]["categories"]["freshness"]["score"] is None
    assert topic["video_priority"]["categories"]["developer_impact"]["score"] == 80


def test_unscored_legacy_topics_last_and_ties_stable():
    a, b, legacy = scored_release("A"), scored_release("B"), scored_release("Legacy")
    del legacy["angles"][0]["editorial"]
    first = rank_updates({"updates": [legacy, b, a]}, NOW, 48, "Audience")
    second = rank_updates({"updates": [a, legacy, b]}, NOW, 48, "Audience")
    assert [t["title"] for t in first["ranked_topics"]] == ["A", "B", "Legacy"]
    assert first["ranked_topics"] == second["ranked_topics"]
    assert first["ranked_topics"][-1]["video_priority"]["overall"] is None


@pytest.mark.parametrize("value", [-1, 101, 50.5, True, "75", None])
def test_editorial_scores_reject_non_integer_or_out_of_bounds(value):
    response = valid({"notes": NOTES})["response"]
    response["angles"][0]["editorial"]["demo_potential"]["score"] = value
    with pytest.raises(ValueError):
        llm_adapter.validate_model(response, {"notes": NOTES}, scored=True)


def test_scored_schema_requires_all_criteria_and_still_checks_quotes():
    response = valid({"notes": NOTES})["response"]
    assert llm_adapter.validate_model(response, {"notes": NOTES}, scored=True)[1] == []
    assert llm_adapter.validate_model(response, {"notes": "different source text"}, scored=True)[1]
    del response["angles"][0]["editorial"]["audience_fit"]
    with pytest.raises(ValueError):
        llm_adapter.validate_model(response, {"notes": NOTES}, scored=True)


def test_production_adapter_selects_scored_schema_and_audience(monkeypatch, config):
    from dataclasses import asdict
    import subprocess
    settings = {**asdict(config.llm), "editorial_scoring": True}
    def fake_run(command, **kwargs):
        assert Path(command[command.index("--output-schema") + 1]).name == "scored.schema.json"
        assert config.llm.audience in kwargs["input"]
        assert "Developer impact: 0" in kwargs["input"]
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(valid({"notes": NOTES})["response"]))
        return subprocess.CompletedProcess(command, 0, json.dumps({"type": "turn.completed"}), "")
    monkeypatch.setattr(llm_adapter.subprocess, "run", fake_run)
    evidence = {"title": "Release", "source_url": "https://github.com/example/agent", "repository": "example/agent", "tag": "v1", "notes": NOTES}
    assert llm_adapter.model_result(evidence, settings, "/test/codex")["status"] == "valid"


def test_audience_change_invalidates_cached_assessment(config, model):
    enrichment.discover_updates([release()], config, enabled=True)
    config.llm.audience = "Experienced SDK maintainers"
    assert enrichment.discover_updates([release()], config, enabled=True)["new_calls"] == 1
    assert len(model) == 2


def hn_story(title="Show HN: Agent reviews", url="https://example.com/reviews"):
    return SourceItem("hacker_news", "123", "hacker_news", "hacker_news_story", title, "", url,
        NOW, None, NOW, metrics={"points": 20, "comments": 12})


def test_hn_page_assessed_with_scores_and_correct_time_basis(config, model, monkeypatch):
    calls = []
    def page(url, _config):
        calls.append(url)
        return {"text": NOTES, "final_url": url, "fetched_at": NOW.isoformat(), "cache_state": "live"}
    monkeypatch.setattr(enrichment, "fetch_page", page)
    story = hn_story()
    result = enrichment.discover_updates([], config, enabled=True, community_items=[story, story])
    assert len(calls) == 1 and result["new_calls"] == 1
    assert model[0]["notes"] == NOTES and model[0]["source_url"] == story.canonical_url
    update = result["updates"][0]
    assert update["source_kind"] == "hacker_news_page" and update["discovery_url"].endswith("123")
    report = build_report(scan_id="hn", started_at=NOW, completed_at=NOW, status="complete", config=config,
        provider_results=[], candidates=[], llm_updates=result)
    topic = report["llm_updates"]["ranked_topics"][0]
    assert topic["video_priority"]["categories"]["freshness"]["score"] == 100
    markdown = render_markdown(report)
    assert "HN submission time, not product launch time" in markdown
    assert "Linked page:" in markdown and "HN discussion" in markdown
    assert enrichment.discover_updates([], config, enabled=True, community_items=[story])["cached_results"] == 1


def test_hn_failures_caps_disabled_mode_and_shared_budget(config, model, monkeypatch):
    calls = []
    def unavailable(url, _config):
        calls.append(url)
        raise enrichment.PageUnavailable("No public evidence")
    monkeypatch.setattr(enrichment, "fetch_page", unavailable)
    stories = [hn_story(url=f"https://example.com/{n}") for n in range(3)]
    config.llm.max_hn_stories = 1
    result = enrichment.discover_updates([release()], config, enabled=True, community_items=stories)
    assert len(calls) == 1 and result["skipped"] == 3 and result["new_calls"] == 1
    assert result["updates"][0]["source_kind"] == "github_release"
    enrichment.discover_updates([], config, enabled=False, community_items=stories)
    config.llm.max_hn_stories = 0
    enrichment.discover_updates([], config, enabled=True, community_items=stories)
    assert len(calls) == 1
    config.llm.max_hn_stories = 5
    config.llm.max_calls_per_scan = 1
    monkeypatch.setattr(enrichment, "fetch_page", lambda url, _: {"text": NOTES, "final_url": url,
        "fetched_at": NOW.isoformat(), "cache_state": "live"})
    result = enrichment.discover_updates([release("new")], config, enabled=True, community_items=stories)
    assert result["new_calls"] == 1 and result["updates"][0]["source_kind"] == "hacker_news_page"
    assert result["skipped"] == 3


def test_hn_uses_page_prompt_not_release_prompt(config, monkeypatch):
    from dataclasses import asdict
    import subprocess
    def fake_run(command, **kwargs):
        assert "community-discovered" in kwargs["input"]
        assert "HN post marks a product launch" in kwargs["input"]
        assert "Developer impact: 0" in kwargs["input"]
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(valid({"notes": NOTES})["response"]))
        return subprocess.CompletedProcess(command, 0, json.dumps({"type": "turn.completed"}), "")
    monkeypatch.setattr(llm_adapter.subprocess, "run", fake_run)
    evidence = {"title": "Show HN", "repository": "", "tag": "", "source_url": "https://example.com", "notes": NOTES}
    settings = {**asdict(config.llm), "editorial_scoring": True, "source_kind": "hacker_news_page"}
    assert llm_adapter.model_result(evidence, settings, "/test/codex")["status"] == "valid"


def test_pipeline_sends_main_list_hn_to_page_enrichment(tmp_path, monkeypatch, model):
    root = Path(__file__).resolve().parents[1]
    path = tmp_path / "config.toml"
    path.write_text((root / "config.example.toml").read_text())
    story = hn_story("Show HN: AI agent reviews for developer tools")
    monkeypatch.setattr(pipeline.official, "collect", lambda *_: ProviderResult("official", "ok", [], NOW))
    monkeypatch.setattr(pipeline.github, "collect_watched", lambda *_: ProviderResult("github_watched", "ok", [], NOW))
    monkeypatch.setattr(pipeline.github, "collect_exploratory", lambda *_: ProviderResult("github_explore", "ok", [], NOW))
    monkeypatch.setattr(pipeline.hackernews, "collect", lambda *_: ProviderResult("hacker_news", "ok", [story], NOW))
    monkeypatch.setattr(pipeline.huggingface, "collect", lambda *_: ProviderResult("huggingface", "ok", [], NOW))
    monkeypatch.setattr(pipeline.youtube, "validate", lambda *_, **kw: ProviderResult("youtube", "disabled", [], NOW))
    monkeypatch.setattr(enrichment, "fetch_page", lambda url, _: {"text": NOTES, "final_url": url,
        "fetched_at": NOW.isoformat(), "cache_state": "live"})
    assert pipeline.run_scan(path, llm=True, no_youtube=True) == 0
    report = json.loads((tmp_path / "reports/latest.json").read_text())
    assert report["recommendations"][0]["observed_signals"][0]["external_id"] == "123"
    assert report["llm_updates"]["ranked_topics"][0]["source_kind"] == "hacker_news_page"
