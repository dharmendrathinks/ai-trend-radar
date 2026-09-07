from __future__ import annotations

import importlib.util
import csv
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("extraction_experiment", ROOT / "experiments/release_extraction/runner.py")
experiment = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(experiment)


@pytest.fixture
def prepared(tmp_path):
    work = tmp_path / "experiment"
    config = experiment.settings(experiment.HERE / "experiment.example.toml")
    experiment.prepare(work, ROOT / "config.example.toml", config, True)
    return work, config


def test_late_feature_separates_input_completeness_from_rules(prepared):
    work, _ = prepared
    case = next(c for c in experiment.read(work / "manifest.json")["cases"] if c["id"] == "control-late-feature")
    evidence = experiment.load_evidence(work, case)
    assert experiment.rule_result(evidence, False)["angles"] == []
    assert "exporting agent" in experiment.rule_result(evidence, True)["angles"][0]["title"]


def test_unknown_or_changed_evidence_never_replays_as_exact(prepared):
    work, _ = prepared
    case = experiment.read(work / "manifest.json")["cases"][0]
    path = work / case["evidence_file"]
    evidence = experiment.read(path)
    evidence["notes"] += "newly edited"
    experiment.write(path, evidence)
    with pytest.raises(ValueError, match="changed"):
        experiment.load_evidence(work, case)
    case["expires_at"] = "2020-01-01T00:00:00Z"
    assert experiment.load_evidence(work, case) is None
    case["expires_at"] = None
    path.unlink()
    assert experiment.load_evidence(work, case) is None


def test_schema_and_literal_quote_rejection():
    response = {"angles": [{"title": "A new API", "developer_value": "API users can paginate.",
                            "evidence_quotes": ["Invented quotation"], "caveats": []}], "abstain_reason": ""}
    checks, errors = experiment.validate_model(response, {"notes": "Adds an API."})
    assert errors and checks == [{"matched": False, "start": None, "end": None}]
    response["angles"][0]["evidence_quotes"] = ["Adds an API."]
    checks, errors = experiment.validate_model(response, {"notes": "## Features\nAdds an API."})
    assert not errors and checks[0]["start"] == 12
    response["abstain_reason"] = "Contradictory abstention"
    with pytest.raises(ValueError, match="Abstention"):
        experiment.validate_model(response, {"notes": "Adds an API."})
    with pytest.raises(ValueError):
        experiment.validate_model({"angles": [], "abstain_reason": "", "score": 100}, {"notes": ""})


def test_rules_only_never_invokes_codex_and_purge_removes_quotes(prepared, monkeypatch):
    work, config = prepared
    def forbidden(*a, **kw):
        pytest.fail("Rules-only comparison must not invoke Codex")
    monkeypatch.setattr(experiment, "model_result", forbidden)
    experiment.run(work, config, False)
    experiment.report(work)
    assert (work / "review.csv").exists()
    assert experiment.read(work / "metrics.json")["counts"]["model_attempted"] == 0
    (work / "findings.md").write_text("Manual findings can contain source-derived claims.")
    experiment.purge(work)
    assert not (work / "evidence").exists() and not (work / "results").exists()
    assert not (work / "review.csv").exists()
    assert not (work / "findings.md").exists()
    experiment.report(work)
    assert "exact replay and quote inspection unavailable" in (work / "comparison.md").read_text()


def test_codex_cache_call_cap_and_failures_are_not_abstentions(prepared, monkeypatch):
    work, config = prepared
    config["max_calls"] = 1
    monkeypatch.setattr(experiment.shutil, "which", lambda _: "/test/codex")
    monkeypatch.setattr(experiment.subprocess, "check_output", lambda *a, **kw: "codex-test")
    calls = []
    def extract(*a):
        calls.append(1)
        return {"status": "failed", "response": None, "errors": ["timeout"], "quote_checks": [], "usage": None}
    monkeypatch.setattr(experiment, "model_result", extract)
    experiment.run(work, config, True)
    experiment.run(work, config, True)
    assert len(calls) == 1
    experiment.report(work)
    metrics = experiment.read(work / "metrics.json")
    assert metrics["counts"]["model_attempted"] == 1
    assert metrics["cohorts"]["control/codex"]["empty_correct"] == 0


def test_codex_invocation_has_no_tools_or_source_credentials(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "never-inherit")
    monkeypatch.setenv("GITHUB_TOKEN", "never-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "do-not-change-saved-login")
    evidence = {"title": "Example", "repository": "example/agent", "tag": "v1", "source_url": "https://example.invalid", "notes": ""}
    config = experiment.settings(experiment.HERE / "experiment.example.toml")
    def fake_run(command, **kwargs):
        assert command[command.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in command
        assert not any(k in kwargs["env"] for k in ("SLACK_WEBHOOK_URL", "GITHUB_TOKEN", "OPENAI_API_KEY"))
        assert kwargs["cwd"] != ROOT and kwargs["timeout"] == 120
        Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps({"angles": [], "abstain_reason": "No notes"}))
        return subprocess.CompletedProcess(command, 0, json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}), "")
    monkeypatch.setattr(experiment.subprocess, "run", fake_run)
    record = experiment.model_result(evidence, config, "/test/codex")
    assert record["status"] == "valid" and record["usage"]["input_tokens"] == 1


def test_tool_activity_invalidates_model_comparison(monkeypatch):
    config = experiment.settings(experiment.HERE / "experiment.example.toml")
    evidence = {"title": "Example", "repository": "example/agent", "tag": "v1", "source_url": "https://example.invalid", "notes": ""}
    def fake_run(command, **kwargs):
        Path(command[command.index("--output-last-message") + 1]).write_text('{"angles":[],"abstain_reason":"No notes"}')
        events = [{"type": "item.completed", "item": {"type": "web_search"}}, {"type": "turn.completed", "usage": {}}]
        return subprocess.CompletedProcess(command, 0, "\n".join(json.dumps(event) for event in events), "")
    monkeypatch.setattr(experiment.subprocess, "run", fake_run)
    result = experiment.model_result(evidence, config, "/test/codex")
    assert result["status"] == "failed" and result["tool_items"] == ["web_search"]


def test_disabled_host_diagnostic_is_not_tool_execution(monkeypatch):
    config = experiment.settings(experiment.HERE / "experiment.example.toml")
    evidence = {"title": "Example", "repository": "example/agent", "tag": "v1", "source_url": "https://example.invalid", "notes": ""}
    def fake_run(command, **kwargs):
        Path(command[command.index("--output-last-message") + 1]).write_text('{"angles":[],"abstain_reason":"No notes"}')
        events = [json.dumps({"type": "item.completed", "item": {"type": "error", "message": "Tool host disabled"}}),
                  json.dumps({"type": "turn.completed", "usage": {}})]
        return subprocess.CompletedProcess(command, 0, "\n".join(events), "")
    monkeypatch.setattr(experiment.subprocess, "run", fake_run)
    result = experiment.model_result(evidence, config, "/test/codex")
    assert result["status"] == "valid" and not result["tool_items"]
    assert result["diagnostic_items"][0]["type"] == "error"


def test_expired_manifest_purges_responses_before_report(prepared):
    work, config = prepared
    experiment.run(work, config, False)
    experiment.report(work)
    manifest = experiment.read(work / "manifest.json")
    manifest["cases"][0]["expires_at"] = "2020-01-01T00:00:00Z"
    experiment.write(work / "manifest.json", manifest)
    assert experiment.main(["report", "--workdir", str(work)]) == 0
    assert not (work / "results").exists() and not (work / "evidence").exists()
    assert experiment.read(work / "metrics.json")["counts"]["unavailable_evidence"] == 8


def test_changed_rules_cannot_be_mixed_with_cached_model_setup(prepared, monkeypatch):
    work, config = prepared
    experiment.run(work, config, False)
    changed = dict(config, reasoning_effort="high")
    monkeypatch.setattr(experiment.shutil, "which", lambda _: "/test/codex")
    monkeypatch.setattr(experiment.subprocess, "check_output", lambda *a, **kw: "codex-test")
    with pytest.raises(ValueError, match="setup changed"):
        experiment.run(work, changed, True)


def test_report_adds_later_model_rows_and_preserves_human_labels(prepared, monkeypatch):
    work, config = prepared
    experiment.run(work, config, False)
    experiment.report(work)
    review = work / "review.csv"
    with review.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0].update(supported="yes", useful="uncertain", caveats_preserved="yes", notes="Keep my review, including commas.\nSecond line.")
    labeled = dict(rows[0])
    with review.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=experiment.REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    monkeypatch.setattr(experiment.shutil, "which", lambda _: "/test/codex")
    monkeypatch.setattr(experiment.subprocess, "check_output", lambda *a, **kw: "codex-test")
    monkeypatch.setattr(experiment, "model_result", lambda *a: {
        "status": "valid", "response": {"angles": [], "abstain_reason": "Mock abstention"},
        "errors": [], "quote_checks": [], "usage": {}, "elapsed_seconds": 0,
    })
    experiment.run(work, config, True)
    experiment.report(work)
    first = review.read_bytes()
    experiment.report(work)
    assert review.read_bytes() == first
    with review.open(newline="") as handle:
        updated = list(csv.DictReader(handle))
    assert labeled in updated
    assert len(updated) == len(rows) + 8
    mapping = experiment.read(work / "blind-key.json")
    assert sum(mapping[row["case_id"]][row["slot"]] == "codex" for row in updated) == 8


def test_review_rejects_changed_output_without_overwriting_labels(tmp_path):
    path = tmp_path / "review.csv"
    row = dict(zip(experiment.REVIEW_FIELDS, ["case", "A", 1, "Original title", "yes", "yes", "yes", "My note"]))
    experiment.update_review(path, [row])
    before = path.read_bytes()
    with pytest.raises(ValueError, match="option changed"):
        experiment.update_review(path, [dict(row, title="Different claim")])
    assert path.read_bytes() == before


def test_prepare_latest_scan_excludes_old_storage_and_accepts_schema_two(tmp_path):
    from ai_trend_radar.db import Database
    from ai_trend_radar.models import ProviderResult, SourceItem

    app_config = tmp_path / "config.toml"
    app_config.write_text((ROOT / "config.example.toml").read_text().replace('database = "data/radar.sqlite3"', 'database = "radar.sqlite3"'))
    database = Database(tmp_path / "radar.sqlite3")
    database.initialize()
    now = datetime.now(UTC)
    for key, when in (("old", now - timedelta(days=1)), ("new", now)):
        notes = "## Features\n- Adds an API for exporting agent traces."
        item = SourceItem("github_watched", key, "github", "github_release", f"Agent {key}", notes,
                          f"https://github.com/example/agent/releases/tag/{key}", when, None, when,
                          full_text=notes, content_complete=True,
                          metrics={"repo_full_name": "example/agent", "release_tag": key})
        database.record_provider_result(ProviderResult("github_watched", "ok", [item], when))
        database.record_scan(scan_id=key, started_at=when, completed_at=when,
                             status="complete", config_fingerprint="test", scoring_version="test",
                             provider_statuses=[], report={})
    settings = experiment.settings(experiment.HERE / "experiment.example.toml")
    for selection in ("latest", "new"):
        work = tmp_path / selection
        experiment.prepare(work, app_config, settings, False, selection)
        manifest = experiment.read(work / "manifest.json")
        assert manifest["scan"]["scan_id"] == "new"
        cases = [c for c in manifest["cases"] if c["cohort"] == "saved_github"]
        assert len(cases) == 1
        assert experiment.load_evidence(work, cases[0])["tag"] == "new"
    with pytest.raises(ValueError, match="latest completed scan"):
        experiment.prepare(tmp_path / "older", app_config, settings, False, "old")
    with pytest.raises(ValueError, match="cannot be combined"):
        experiment.prepare(tmp_path / "controls", app_config, settings, True, "latest")
    with database.connect() as cx:
        assert cx.execute("PRAGMA user_version").fetchone()[0] == 2
