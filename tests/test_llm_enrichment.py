from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from ai_trend_radar import cli, enrichment, pipeline
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
    report = build_report(scan_id="test", started_at=NOW, completed_at=NOW, status="complete",
        config=config, provider_results=[], candidates=[], llm_updates=data)
    markdown = render_markdown(report)
    assert report["schema_version"] == "2.1"
    assert markdown.index("## LLM-discovered updates") < markdown.index("## Provider status") < markdown.index("## Top Opportunities")
    assert NOTES in markdown and "No benchmark" in markdown and "not change Discovery Priority" in markdown
    assert report["recommendations"] == []


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
