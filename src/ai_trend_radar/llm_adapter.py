"""Bounded Codex release extraction with schema and literal-source validation."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any

HERE = Path(__file__).with_name("llm_assets")
ADAPTER_VERSION = "release-llm-v1"
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
    elif kind == "integer":
        if type(value) is not int or not schema.get("minimum", 0) <= value <= schema.get("maximum", 100):
            raise ValueError("Response integer does not match schema bounds")
    else:
        raise ValueError("Unsupported schema type")


def validate_model(response: Any, evidence: dict[str, Any], *, scored: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    validate_schema(response, read(HERE / ("scored.schema.json" if scored else "response.schema.json")))
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
    schema = "scored.schema.json" if config.get("editorial_scoring") else "response.schema.json"
    command = [binary, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
               "--sandbox", "read-only", "--cd", str(directory), "--model", config["model"],
               "--json", "--output-schema", str(HERE / schema),
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
    community = config.get("source_kind") == "hacker_news_page"
    prompt = (HERE / ("community.md" if community else "prompt.md")).read_text()
    if config.get("editorial_scoring"):
        prompt += "\n\n" + (HERE / "editorial.md").read_text()
        supplied["audience"] = config["audience"]
    prompt += "\n\nUNTRUSTED SOURCE EVIDENCE (JSON):\n" + json.dumps(supplied, ensure_ascii=False)
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
                    checks, errors = validate_model(record["response"], evidence, scored=bool(config.get("editorial_scoring")))
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
