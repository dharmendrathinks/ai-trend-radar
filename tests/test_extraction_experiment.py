from __future__ import annotations

import importlib.util
import json
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
