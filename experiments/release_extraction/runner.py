"""Opt-in extraction comparison. Never imports or invokes the scan pipeline."""
from __future__ import annotations

import argparse
import csv
from datetime import UTC, datetime
from hashlib import sha256
import inspect
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import tomllib
from typing import Any

from ai_trend_radar.config import load_config
from ai_trend_radar.models import Candidate, SourceItem
from ai_trend_radar.reports import _atomic_write
from ai_trend_radar import topics
from ai_trend_radar.utils import clean_text

HERE = Path(__file__).resolve().parent
ARMS = ("rules_summary", "rules_full", "codex")
REVIEW_FIELDS = ["case_id", "slot", "angle", "title", "supported", "useful", "caveats_preserved", "notes"]
DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "apps", "plugins", "hooks", "memories",
    "browser_use", "browser_use_external", "in_app_browser", "computer_use",
    "image_generation", "view_image", "multi_agent", "multi_agent_v2",
    "code_mode", "code_mode_host", "goals", "skill_search", "skill_mcp_dependency_install",
    "sleep_tool", "in_app_local_automation",
)


def digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read(path: Path) -> Any:
    return json.loads(path.read_text())


def write(path: Path, value: Any) -> None:
    _atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def settings(path: Path) -> dict[str, Any]:
    config = tomllib.loads(path.read_text())
    for key in ("max_calls", "timeout_seconds", "max_input_chars", "max_saved_releases"):
        if type(config.get(key)) is not int or config[key] <= 0:
            raise ValueError(f"{key} must be a positive integer")
    if not config.get("model") or config.get("reasoning_effort") not in {"low", "medium", "high"}:
        raise ValueError("Set an explicit model and reasoning_effort (low/medium/high)")
    return config


def prepare(work: Path, app_config: Path, experiment_config: dict[str, Any], controls_only: bool,
            scan_id: str | None = None) -> None:
    if work.exists():
        raise ValueError("Use a new work directory; preparation never overwrites a corpus")
    if controls_only and scan_id:
        raise ValueError("--scan-id cannot be combined with --controls-only")
    config = load_config(app_config)
    exported: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for control in read(HERE / "controls.json"):
        notes = control["notes"] * control.get("repeat_prefix", 1) + control.get("append", "")
        evidence = {"title": "Example Agent v1.2.3", "repository": "example/agent", "tag": "v1.2.3",
                    "source_url": "https://example.invalid/release", "published_at": "2026-09-01T00:00:00Z",
                    "captured_at": None, "notes": notes, "summary": clean_text(notes),
                    "input_complete": True, "topics_config": config.topics}
        exported.append(({"id": "control-" + control["id"], "cohort": "control",
                          "purpose": control["purpose"], "expected_quotes": control["expected_quotes"]}, evidence))
    excluded = {"incomplete_or_legacy": 0, "over_input_limit": 0, "over_case_limit": 0}
    selected_scan = None
    if not controls_only:
        # No Database.initialize(), cache refresh, or write connection to production state.
        uri = config.database_path.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True) as cx:
            query = "SELECT payload_json FROM source_items WHERE item_type='github_release'"
            params = ()
            if scan_id:
                latest = cx.execute("SELECT scan_id, started_at FROM scans ORDER BY completed_at DESC LIMIT 1").fetchone()
                if latest is None or scan_id not in {"latest", latest[0]}:
                    raise ValueError("Only the latest completed scan can be exported; older source-item snapshots are not retained")
                selected_scan = {"scan_id": latest[0], "started_at": latest[1]}
                query += " AND last_observed_at = ?"
                params = (latest[1],)
            rows = cx.execute(query + " ORDER BY provider, external_id", params).fetchall()
        saved_count = 0
        for (raw,) in rows:
            item = json.loads(raw)
            if item.get("full_text") is None or item.get("content_complete") is not True:
                excluded["incomplete_or_legacy"] += 1
                continue
            if len(item["full_text"]) > experiment_config["max_input_chars"]:
                excluded["over_input_limit"] += 1
                continue
            if saved_count >= experiment_config["max_saved_releases"]:
                excluded["over_case_limit"] += 1
                continue
            if not str(item["canonical_url"]).startswith("https://github.com/"):
                raise ValueError("Only GitHub release URLs are supported by this exporter")
            evidence = {"title": item["title"], "repository": item["metrics"]["repo_full_name"],
                        "tag": item["metrics"]["release_tag"], "source_url": item["canonical_url"],
                        "published_at": item.get("published_at"), "captured_at": item.get("body_fetched_at"),
                        "notes": item["full_text"], "summary": item["summary"], "input_complete": True,
                        "topics_config": config.topics}
            exported.append(({"id": "github-" + digest([item["external_id"], item["full_text"]])[:16],
                              "cohort": "saved_github", "purpose": "Saved complete release; human evaluation pending"}, evidence))
            saved_count += 1
    work.mkdir(parents=True)
    cases = []
    for case, evidence in exported:
        case.update(evidence_file=f"evidence/{case['id']}.json", evidence_sha256=digest(evidence),
                    evidence_status="available", expires_at=None, source_url=evidence["source_url"],
                    captured_at=evidence["captured_at"], input_chars=len(evidence["notes"]))
        write(work / case["evidence_file"], evidence)
        cases.append(case)
    selection = "Complete GitHub releases observed in the latest scan" if selected_scan else "All complete saved GitHub releases"
    write(work / "manifest.json", {"schema_version": 1, "created_at": datetime.now(UTC).isoformat(),
          "scan": selected_scan,
          "selection": selection + " in stable source order, within disclosed caps; no ranking-based selection. Observed again does not mean a previously unseen release",
          "excluded": excluded, "source_policy": "GitHub release notes and repository-authored controls only; no YouTube or Reddit export. Operators must confirm permission to send saved notes to a model; a GitHub URL alone does not establish public visibility or redistribution rights.",
          "cases": cases})
    print(f"Prepared {len(cases)} cases; exclusions: {excluded}", flush=True)


def load_evidence(work: Path, case: dict[str, Any]) -> dict[str, Any] | None:
    if case["evidence_status"] != "available":
        return None
    if case.get("expires_at") and datetime.fromisoformat(case["expires_at"].replace("Z", "+00:00")) <= datetime.now(UTC):
        return None
    path = work / case["evidence_file"]
    if not path.is_file():
        return None
    evidence = read(path)
    if digest(evidence) != case["evidence_sha256"]:
        raise ValueError("Evidence changed after preparation; prepare a new corpus")
    return evidence


def rule_result(evidence: dict[str, Any], full: bool) -> dict[str, Any]:
    when = datetime.fromisoformat((evidence["published_at"] or "2026-01-01T00:00:00Z").replace("Z", "+00:00"))
    item = SourceItem("github_watched", evidence["repository"] + "@" + evidence["tag"], "github", "github_release",
                      evidence["title"], evidence["summary"], evidence["source_url"], when, None, when,
                      authority="official", full_text=evidence["notes"] if full else None,
                      content_complete=full, metrics={"repo_full_name": evidence["repository"], "release_tag": evidence["tag"]})
    candidate = Candidate("experiment", item.title, None, when, [item], ["github"])
    topic = topics.extract_release_topic(candidate, evidence["topics_config"])
    angles = [{"title": angle["title"], "developer_value": "", "evidence_quotes": [angle["evidence"]["text"]], "caveats": []}
              for angle in [topic["primary_angle"], *topic["alternative_angles"]] if angle["evidence"]]
    return {"angles": angles, "abstain_reason": topic["fallback_reason"] or "", "raw_topic": topic,
            "quote_comparison": "rules normalize HTML/whitespace before extraction; model quotes must match literal captured text"}


def validate_schema(value: Any, schema: dict[str, Any]) -> None:
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict) or set(value) != set(schema["required"]):
            raise ValueError("Response fields do not match the schema")
        for key, child in value.items():
            validate_schema(child, schema["properties"][key])
    elif kind == "array":
        if not isinstance(value, list) or not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 999):
            raise ValueError("Response array exceeds schema bounds")
        for child in value:
            validate_schema(child, schema["items"])
    elif kind == "string":
        if not isinstance(value, str) or not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", 99999):
            raise ValueError("Response string does not match schema bounds")
    else:
        raise ValueError("Unsupported schema type")


def validate_model(response: Any, evidence: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    validate_schema(response, read(HERE / "response.schema.json"))
    if bool(response["angles"]) == bool(response["abstain_reason"].strip()):
        raise ValueError("Abstention reason must be present only when there are no angles")
    checks = []
    for angle in response["angles"]:
        for quote in angle["evidence_quotes"]:
            start = evidence["notes"].find(quote)
            checks.append({"matched": start >= 0, "start": start if start >= 0 else None,
                           "end": start + len(quote) if start >= 0 else None})
    return checks, ([] if all(c["matched"] for c in checks) else ["One or more evidence quotes are not literal source spans"])


def codex_command(binary: str, directory: Path, config: dict[str, Any]) -> list[str]:
    command = [binary, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
               "--sandbox", "read-only", "--cd", str(directory), "--model", config["model"],
               "--json", "--output-schema", str(HERE / "response.schema.json"),
               "--output-last-message", str(directory / "response.json"),
               "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
               "-c", "project_doc_max_bytes=0", "-c", "skills.bundled.enabled=false",
               "-c", "model_reasoning_effort=" + json.dumps(config["reasoning_effort"])]
    for feature in DISABLED_FEATURES:
        command.extend(["--disable", feature])
    return [*command, "-"]


def model_result(evidence: dict[str, Any], config: dict[str, Any], binary: str) -> dict[str, Any]:
    # Do not load .env or inherit discovery/Slack tokens. CLI reuses its own login.
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "CODEX_HOME", "SYSTEMROOT", "WINDIR",
               "APPDATA", "LOCALAPPDATA", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    supplied = {key: evidence[key] for key in ("title", "repository", "tag", "source_url", "notes")}
    prompt = (HERE / "prompt.md").read_text() + "\n\nUNTRUSTED RELEASE EVIDENCE (JSON):\n" + json.dumps(supplied, ensure_ascii=False)
    started = time.monotonic()
    record: dict[str, Any] = {"status": "failed", "response": None, "quote_checks": [], "errors": [],
                              "usage": None, "tool_items": [], "diagnostic_items": [], "turn_completed": False,
                              "elapsed_seconds": None}
    with tempfile.TemporaryDirectory(prefix="radar-extraction-") as temporary:
        directory = Path(temporary)
        try:
            process = subprocess.run(codex_command(binary, directory, config), input=prompt, text=True,
                                     capture_output=True, cwd=directory, env=env, timeout=config["timeout_seconds"])
        except subprocess.TimeoutExpired:
            record["errors"] = ["Codex timeout; not retried automatically"]
        else:
            record["exit_code"] = process.returncode
            # Raw stderr can include account/environment detail. Keep only a hash.
            record["stderr_sha256"] = sha256(process.stderr.encode()).hexdigest()
            for line in process.stdout.splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "turn.completed":
                    record["usage"] = event.get("usage")
                    record["turn_completed"] = True
                elif event.get("type") in {"turn.failed", "error"}:
                    record["errors"].append("Codex reported a failed turn or execution error")
                item = event.get("item", {})
                if item.get("type") == "error":
                    record["diagnostic_items"].append({"type": "error", "message_sha256": digest(item.get("message"))})
                elif item.get("type") and item["type"] not in {"agent_message", "reasoning"}:
                    record["tool_items"].append(item["type"])
            output = directory / "response.json"
            if process.returncode:
                record["errors"].append(f"Codex exited {process.returncode}; output is not a valid completed extraction")
            if not record["turn_completed"]:
                record["errors"].append("No completed-turn event was recorded")
            if record["tool_items"]:
                record["errors"].append("Tool activity detected; excluded from the no-tools comparison")
            if output.is_file():
                try:
                    record["response"] = read(output)
                    checks, errors = validate_model(record["response"], evidence)
                    record["quote_checks"] = checks
                    record["errors"].extend(errors)
                except (ValueError, TypeError, KeyError):
                    record["raw_output"] = output.read_text()
                    record["errors"].append("Response failed JSON/schema/abstention validation")
            else:
                record["errors"].append("No structured response was produced")
            if not record["errors"]:
                record["status"] = "valid"
    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return record


def run(work: Path, config: dict[str, Any], with_codex: bool) -> None:
    manifest = read(work / "manifest.json")
    binary = shutil.which("codex") if with_codex else None
    if with_codex and not binary:
        raise ValueError("Install and log in to Codex CLI first, or run without --codex")
    version = subprocess.check_output([binary, "--version"], text=True).strip() if binary else None
    identity = {"settings": config, "codex_version": version, "prompt_sha256": digest((HERE / "prompt.md").read_text()),
                "schema_sha256": digest(read(HERE / "response.schema.json")),
                "extractor_version": topics.EXTRACTION_VERSION, "extractor_sha256": digest(inspect.getsource(topics)),
                "runner_sha256": digest(Path(__file__).read_text()), "model_requested": config["model"],
                "resolved_model_snapshot": None, "disabled_features": list(DISABLED_FEATURES)}
    settings_file = work / ("run-settings.json" if with_codex else "rules-settings.json")
    other_file = work / ("rules-settings.json" if with_codex else "run-settings.json")
    if other_file.exists():
        previous = read(other_file)
        common = {k: v for k, v in identity.items() if k != "codex_version"}
        if common != {k: v for k, v in previous.items() if k != "codex_version"}:
            raise ValueError("Rule and model setup changed; use a new work directory")
    if settings_file.exists() and read(settings_file) != identity:
        raise ValueError("Experiment settings/code changed; use a new work directory to avoid mixing results")
    write(settings_file, identity)
    _atomic_write(work / "runner.used.py", Path(__file__).read_text())
    _atomic_write(work / "prompt.used.md", (HERE / "prompt.md").read_text())
    write(work / "schema.used.json", read(HERE / "response.schema.json"))
    previous_calls = sum(read(path).get("arms", {}).get("codex", {}).get("status") in {"valid", "failed"}
                         for path in (work / "results").glob("*.json"))
    calls = 0
    failures = 0
    for case in manifest["cases"]:
        evidence = load_evidence(work, case)
        if evidence is None:
            print(f"{case['id']}: unavailable/expired evidence; skipped", flush=True)
            continue
        path = work / "results" / (case["id"] + ".json")
        record = read(path) if path.exists() else {"evidence_sha256": case["evidence_sha256"], "arms": {}}
        if record["evidence_sha256"] != case["evidence_sha256"]:
            raise ValueError("Cached result does not match the prepared evidence")
        record["arms"]["rules_summary"] = rule_result(evidence, False)
        record["arms"]["rules_full"] = rule_result(evidence, True)
        if with_codex and "codex" not in record["arms"]:
            if previous_calls + calls >= config["max_calls"] or failures >= 3:
                print("Call limit or three consecutive failures reached; remaining model cases are pending", flush=True)
                write(path, record)
                break
            if len(evidence["notes"]) > config["max_input_chars"]:
                record["arms"]["codex"] = {"status": "skipped", "errors": ["Input exceeds configured limit; not truncated"], "response": None}
            else:
                calls += 1
                record["arms"]["codex"] = model_result(evidence, config, binary)
                failures = failures + 1 if record["arms"]["codex"]["status"] != "valid" else 0
        write(path, record)
        model = record["arms"].get("codex", {})
        print(f"{case['id']}: summary={len(record['arms']['rules_summary']['angles'])}, full={len(record['arms']['rules_full']['angles'])}, codex={model.get('status', 'not run')}", flush=True)
    print(f"New Codex calls: {calls}. Ranking and production databases were not updated.", flush=True)


def arm_output(record: dict[str, Any], arm: str) -> dict[str, Any] | None:
    value = record.get("arms", {}).get(arm)
    if arm == "codex":
        return value.get("response") if value and value.get("status") == "valid" else None
    return value


def update_review(path: Path, rows: list[dict[str, Any]]) -> None:
    """Append newly completed options while preserving existing human labels."""
    existing = []
    if path.exists():
        with path.open(newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != REVIEW_FIELDS:
                raise ValueError("Review CSV columns changed; preserve the original columns before regenerating")
            existing = list(reader)
    def key(row):
        return tuple(str(row[field]) for field in ("case_id", "slot", "angle"))
    indexed = {key(row): row for row in existing}
    if len(indexed) != len(existing):
        raise ValueError("Review CSV contains duplicate option rows; existing file was preserved")
    for row in rows:
        previous = indexed.get(key(row))
        if previous is not None:
            if previous["title"] != row["title"]:
                raise ValueError("Review option changed; use a new corpus to avoid transferring labels to different output")
        else:
            existing.append(row)
            indexed[key(row)] = row
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=REVIEW_FIELDS)
    writer.writeheader()
    writer.writerows(existing)
    _atomic_write(path, buffer.getvalue())


def report(work: Path) -> None:
    manifest = read(work / "manifest.json")
    lines = ["# Release extraction shadow experiment", "", f"Corpus prepared: {manifest['created_at']}", "",
             "This is an extraction pilot, not a prospective discovery benchmark. Quote matching is not factual entailment or human usefulness. No ranking changes.", "",
             f"Selection: {manifest['selection']}. Exclusions: `{json.dumps(manifest['excluded'])}`.", "",
             "Summary rules replay the saved 2,000-character display input through the current selection rules. Full rules and Codex receive the same captured complete notes. Legacy selection logic was unchanged by the v1.1→v1.2 input change.", "",
             "## Mechanical results", "", "| Cohort | Arm | Completed cases | Cases with topics | Required control spans hit/total | Correct empty control cases |", "|---|---|---:|---:|---|---:|"]
    stats = {(cohort, arm): {"completed": 0, "with_topics": 0, "hits": 0, "expected": 0, "empty_correct": 0}
             for cohort in ("control", "saved_github") for arm in ARMS}
    details, blind, mapping, rows = [], ["# Blinded extraction review", "", "Judge each option using the linked captured notes. Slots hide arm names but writing style may reveal them. Do not use this to infer early detection.", ""], {}, []
    counts = {"unavailable_evidence": 0, "model_attempted": 0, "model_valid": 0, "model_quotes": 0, "literal_matches": 0, "input_tokens": 0, "output_tokens": 0, "elapsed_seconds": 0.0}
    for case in manifest["cases"]:
        evidence = load_evidence(work, case)
        path = work / "results" / (case["id"] + ".json")
        if evidence is None:
            counts["unavailable_evidence"] += 1
            details.append(f"### {case['id']}\n\nEvidence expired or unavailable; exact replay and quote inspection unavailable.\n")
            continue
        record = read(path) if path.exists() else {"arms": {}}
        model = record["arms"].get("codex", {})
        if model.get("status") in {"valid", "failed"}:
            counts["model_attempted"] += 1
            counts["model_valid"] += model["status"] == "valid"
            checks = model.get("quote_checks", [])
            counts["model_quotes"] += len(checks)
            counts["literal_matches"] += sum(c["matched"] for c in checks)
            counts["elapsed_seconds"] += model.get("elapsed_seconds", 0)
            for key in ("input_tokens", "output_tokens"):
                counts[key] += (model.get("usage") or {}).get(key, 0)
        details.extend([f"### {case['id']}: {evidence['title']}", "", f"{case['purpose']} · [Source]({case['source_url']}) · Captured: {case['captured_at'] or 'authored control'}", ""])
        blind.extend([f"## {case['id']}", "", f"[Captured evidence]({case['evidence_file']})", ""])
        order = sorted(ARMS, key=lambda arm: digest([case["id"], arm, "blind-v1"]))
        mapping[case["id"]] = dict(zip("ABC", order))
        for arm in ARMS:
            output = arm_output(record, arm)
            stat = stats[case["cohort"], arm]
            if case["cohort"] == "control":
                stat["expected"] += len(case["expected_quotes"])
            if output is None:
                details.extend([f"**{arm}:** {model.get('status', 'not run') if arm == 'codex' else 'not run'}; {model.get('errors', []) if arm == 'codex' else ''}", ""])
                continue
            stat["completed"] += 1
            stat["with_topics"] += bool(output["angles"])
            if case["cohort"] == "control":
                quotes = [clean_text(q, limit=100000) for angle in output["angles"] for q in angle["evidence_quotes"]]
                stat["hits"] += sum(any(clean_text(q) in actual for actual in quotes) for q in case["expected_quotes"])
                stat["empty_correct"] += not case["expected_quotes"] and not output["angles"]
            details.extend([f"**{arm}:**", "", "```json", json.dumps(output if arm == "codex" else {k: output[k] for k in ('angles', 'abstain_reason')}, indent=2, ensure_ascii=False), "```", ""])
        for slot, arm in mapping[case["id"]].items():
            output = arm_output(record, arm)
            blind.extend([f"### Option {slot}", ""])
            if output is None:
                blind.extend(["No valid completed output.", ""])
                continue
            if not output["angles"]:
                blind.extend(["Abstained: " + output["abstain_reason"], ""])
                rows.append({"case_id": case["id"], "slot": slot, "angle": 0, "title": "[abstained]",
                             "supported": "", "useful": "", "caveats_preserved": "", "notes": ""})
            for index, angle in enumerate(output["angles"], 1):
                blind.extend([f"{index}. {angle['title']}", "", angle["developer_value"], ""])
                blind.extend("> " + q.replace("\n", "\n> ") for q in angle["evidence_quotes"])
                blind.extend(["", "Caveats: " + "; ".join(angle["caveats"]), ""])
                rows.append({"case_id": case["id"], "slot": slot, "angle": index, "title": angle["title"],
                             "supported": "", "useful": "", "caveats_preserved": "", "notes": ""})
    for (cohort, arm), stat in stats.items():
        span = f"{stat['hits']}/{stat['expected']}" if cohort == "control" else "human labels pending"
        lines.append(f"| {cohort} | {arm} | {stat['completed']} | {stat['with_topics']} | {span} | {stat['empty_correct'] if cohort == 'control' else '—'} |")
    lines.extend(["", "Required-span hits and empty-case correctness use repository-authored synthetic controls, not an independently labeled benchmark. Failed/unrun model cases are not successful abstentions.", "",
                  f"Codex valid completed cases: {counts['model_valid']}/{counts['model_attempted']}; literal quote matches: {counts['literal_matches']}/{counts['model_quotes']}. Evidence unavailable: {counts['unavailable_evidence']}.", "",
                  f"Reported tokens: {counts['input_tokens']} input, {counts['output_tokens']} output. Recorded model-call time: {counts['elapsed_seconds']:.1f}s. Missing usage is not zero-cost; dollar cost is unknown under the local Codex account.", "",
                  "## Interpretation limits", "", "No human usefulness labels have been aggregated. Review blind-review.md and fill review.csv before claiming semantic precision, usefulness, or a winner. Evidence-span matching cannot detect a title that contradicts a correctly quoted source. Saved releases are a small convenience sample from the watchlist, not unknown-project discovery or prospective evaluation. Model knowledge can contaminate retrospective interpretation. Model aliases and CLI defaults can change; replay saved responses instead of assuming identical regeneration.", "", "## Cases", "", *details])
    write(work / "metrics.json", {"counts": counts, "cohorts": {f"{c}/{a}": s for (c,a),s in stats.items()}, "human_evaluation": "not aggregated"})
    _atomic_write(work / "comparison.md", "\n".join(lines) + "\n")
    _atomic_write(work / "blind-review.md", "\n".join(blind) + "\n")
    write(work / "blind-key.json", mapping)
    update_review(work / "review.csv", rows)
    print(f"Comparison: {work / 'comparison.md'}", flush=True)


def purge(work: Path, reason: str = "operator purged local evidence and enrichment payloads") -> None:
    manifest = read(work / "manifest.json")
    for case in manifest["cases"]:
        case["evidence_status"] = "unavailable"
        case["unavailable_reason"] = reason
        case.pop("expected_quotes", None)
    write(work / "manifest.json", manifest)
    for name in ("evidence", "results"):
        if (work / name).exists():
            shutil.rmtree(work / name)
    for name in ("comparison.md", "blind-review.md", "blind-key.json", "review.csv", "findings.md"):
        (work / name).unlink(missing_ok=True)
    print("Purged evidence and response/review payloads. Manifest hashes/settings/aggregate metrics remain; exact replay is unavailable.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "run", "report", "purge"))
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config.toml"))
    parser.add_argument("--experiment-config", type=Path, default=HERE / "experiment.example.toml")
    parser.add_argument("--controls-only", action="store_true")
    parser.add_argument("--scan-id", help="prepare only releases observed in the latest completed scan (ID or 'latest')")
    parser.add_argument("--codex", action="store_true", help="explicitly spend Codex usage on the model arm")
    args = parser.parse_args(argv)
    try:
        config = settings(args.experiment_config)
        if args.command in {"run", "report"}:
            manifest = read(args.workdir / "manifest.json")
            if any(c.get("expires_at") and datetime.fromisoformat(c["expires_at"].replace("Z", "+00:00")) <= datetime.now(UTC)
                   for c in manifest["cases"] if c["evidence_status"] == "available"):
                purge(args.workdir, "configured evidence retention deadline expired; entire pilot payloads purged")
        if args.command == "prepare":
            prepare(args.workdir, args.config, config, args.controls_only, args.scan_id)
        elif args.command == "run":
            run(args.workdir, config, args.codex)
        elif args.command == "report":
            report(args.workdir)
        else:
            purge(args.workdir)
        return 0
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"Experiment failed: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
