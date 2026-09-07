# Release extraction shadow experiment

Normal scans now use an additional editorial rubric (`llm_assets/editorial.md`) and `scored.schema.json` to assess video-topic priority. This harness intentionally retains the extraction-only `prompt.md` and `response.schema.json` through the shared adapter's default mode. Its review results do not validate editorial scores or topic ranking.

Compare three extraction methods on the same saved releases. This standalone, opt-in evaluation harness shares the packaged adapter with normal scans, but its saved outputs do not alter scores, topicability gates, review decisions, normal reports, the Slack brief, or your schedule. For the shipped optional report section, see the [normal-scan guide](../../README.md#optional-llm-discovered-updates). The shared prompt and response schema live in `src/ai_trend_radar/llm_assets/`; edit them there, not in this directory.

| Arm | Input and method |
|---|---|
| `rules_summary` | Current deterministic selection rules reading the saved display summary (normally capped at 2,000 characters), without `full_text` |
| `rules_full` | The same rules reading the complete captured release notes |
| `codex` | Codex reading those same complete notes, constrained to zero to three topics with literal supporting quotations |

The summary arm is an input ablation, not historical backtesting. At introduction, the selection rules match the old v1.1 extractor: v1.2 changed the input to full text and added completeness metadata. The experiment records the actual extractor version and source hash; future changes must not be described as an exact replay of old rules.

## Requirements and boundaries

Use a repository checkout with the normal dependencies installed. The deterministic arms need no model or credentials. The model arm additionally needs a recent Codex CLI with `exec`, `--ignore-user-config`, and `--output-schema`; this adapter was tested with **codex-cli 0.153.4**. Log in locally with `codex login` and check `codex login status`.

Codex runs locally as a client and sends the selected notes to its model service. This adapter reuses the CLI's saved login; it does not read Radar's `.env` or inherit GitHub, Hugging Face, YouTube, Slack, or API-key environment variables. Do not copy authentication into the repository or public CI. Model use consumes your Codex account allowance; dollar cost is not inferred from token counts.

The adapter uses an isolated temporary working directory, read-only sandbox, disabled shell/browser/app/plugin/agent tools, no web search, and no project instructions. Any observed action-tool event invalidates that result. A non-action diagnostic about a deliberately disabled tool host is recorded separately. These settings constrain the comparison; literal quotation checks still cannot establish semantic correctness.

Official references: [Codex noninteractive mode](https://learn.chatgpt.com/docs/non-interactive-mode) and [configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

## Run a pilot

Run these commands from the repository root. Choose a new work directory for each corpus; preparation refuses to overwrite one.

```bash
# Export saved complete GitHub releases plus eight synthetic controls.
# The source database is opened read-only; no source requests are made.
uv run python experiments/release_extraction/runner.py prepare \
  --workdir reports/experiments/my-pilot

# Run the two deterministic arms only; no model calls.
uv run python experiments/release_extraction/runner.py run \
  --workdir reports/experiments/my-pilot

# Explicitly run the Codex arm, using the same prepared evidence.
uv run python experiments/release_extraction/runner.py run \
  --workdir reports/experiments/my-pilot --codex

# Build the comparison, machine counts, and review materials.
uv run python experiments/release_extraction/runner.py report \
  --workdir reports/experiments/my-pilot
```

If you have no saved scans yet, add `--controls-only --config config.example.toml` to `prepare`. Otherwise, `--config` chooses the Radar configuration containing the database path and topic-rule settings. It does not start a scan.

For a fresh scan followed by LLM review, first run `uv run ai-trend-radar scan --no-llm` and add `--scan-id latest` to the preparation command. This selects only GitHub releases observed during that completed scan, plus the eight controls. The manifest records the scan ID. `--no-llm` prevents model calls even when local configuration enables them. The scan sends no Slack messages unless `--slack` is supplied. Run the explicit `--codex` and `report` steps above to produce the comparison.

Prepare immediately after the scan: stored source items are updated in place, so `--scan-id` accepts only the latest completed scan's ID or `latest`. Seeing a release in a new scan does not make it an unseen evaluation case; compare its source URL against prior pilots before calling it a holdout.

Inspect `manifest.json` and the selected evidence before `--codex`. The exporter accepts only stored GitHub releases with explicit complete notes; legacy summaries and capped inputs are excluded and counted. A GitHub URL alone does not establish that a repository is public or that you have permission to send its content to a model.

## Limits and reproducibility

[experiment.example.toml](experiment.example.toml) pins a model name, reasoning effort, a 24-call limit per work directory, a 120-second timeout per call, a 20,000-character input cap, and a maximum of 20 saved releases. Eight repository-authored controls are added separately. The example therefore does not promise that all possible cases fit within the model-call limit; pending cases remain visible.

To change settings, copy the example to `experiment.local.toml` in this directory, edit it, and pass `--experiment-config experiments/release_extraction/experiment.local.toml`. That local file is Git-ignored. Oversized model inputs are skipped, never silently truncated. The supplied model is a tested starting point, not a requirement for Radar itself; select a model supported by your Codex account.

Each model response is saved once. Repeating `run --codex` resumes remaining cases within the total call cap and reuses recorded responses, including failures; it does not automatically pay for repair attempts. Three consecutive failed validations/executions stop that invocation. A changed model, prompt, schema, CLI version, configuration, or runner requires a new work directory so comparisons do not silently mix versions. Counts and failure states appear in the output even if the runner command itself completes successfully.

Model aliases can change behind the same name. The requested name is recorded, but an immutable resolved model snapshot is not available from this adapter. Reproducibility means inspecting the saved response and evidence, not promising identical regeneration. `runner.used.py`, `prompt.used.md`, `schema.used.json`, and the source/config hashes preserve the experiment setup. Usage totals cover calls for which Codex reports usage; missing usage does not mean free execution.

## What to inspect

| Artifact | Purpose |
|---|---|
| `manifest.json` | Case selection, exclusions, provenance, hashes, and evidence availability |
| `evidence/*.json` | Captured notes and the saved summary used for the input comparison |
| `results/*.json` | Both rule outputs, original model response, literal quote offsets, failures, timing, and usage |
| `run-settings.json` | Model/config/CLI versions and prompt/schema/source hashes |
| `comparison.md` | Named-arm comparison and per-case outputs |
| `metrics.json` | Mechanical counts separated into synthetic controls and saved releases |
| `blind-review.md` | Options A/B/C per case without arm names; writing style may still reveal the arm |
| `review.csv` | Human judgments; report regeneration preserves existing labels and appends newly completed options |
| `blind-key.json` | Arm mapping; keep hidden during the first review |

You can generate review materials before or after the model cases. Regenerating the report appends missing rows by case, blinded slot, and angle, preserving existing labels and notes without duplicating rows. If an existing option's title changes, regeneration fails instead of transferring its judgments to a different claim. Keep the original CSV columns. Human judgments are not aggregated automatically.

## Evaluation protocol

The eight controls cover an obvious feature, a feature beyond the summary cap, prose-only notes, routine maintenance, empty notes, a negated capability, a qualified preview, and embedded instructions. Their expected spans are authored acceptance controls, not an independent gold-standard dataset.

The automated report measures:

- Completed versus failed/unrun cases, separately for each arm and cohort.
- How often an arm emits at least one topic. More topics is not necessarily better.
- Required control evidence spans recovered, and correct abstention on the three empty-topic controls.
- Exact model quotation matches against captured text. Existing rules normalize HTML and whitespace, so their quotations are evaluated on that stated basis.
- Reported token use and call latency, with unknown dollar cost.

A valid quote can still accompany an unsupported headline. In `blind-review.md`, compare every title and explanation with the full evidence, then fill `review.csv`:

| Field | Human judgment |
|---|---|
| `supported` | `yes` / `no` / `uncertain`: does the evidence support the entire claim? For angle 0, is abstention justified? |
| `useful` | `yes` / `no` / `uncertain`: would this change deserve investigation for your developer audience? |
| `caveats_preserved` | `yes` / `no` / `not_applicable`: are previews, prerequisites, limits, and publisher-only claims retained? |
| `notes` | Missing substantive changes, duplicates, the preferable primary angle, and reasons for rejection |

Do not use the extracting model as its own primary judge. For broader precision/recall claims, have humans label useful changes from the source notes before seeing any arm outputs; use a second reviewer on disagreements. The current span-hit metric is not semantic recall, and the CSV is a review aid rather than a completed evaluator.

The current prompt prioritizes demonstrable capabilities, keeps substantive narrow improvements as secondary candidates, and excludes ordinary regression repairs unless the notes establish a consequential developer decision. This is an editorial hypothesis informed by a small, unblinded user review, not a calibrated usefulness classifier. Every headline and limitation still needs source support. Re-running authored controls after a prompt edit is a development check; it is not held-out evidence that recommendation quality improved.

For the next prospective round, prepare a fresh corpus after each normal scan and record human judgments before subsequent community outcomes. Keep the prompt fixed during that round and hold out new releases when revising it. Development can continue while observations accumulate; no eight-week prerequisite is imposed on using the deterministic product.

## Evidence retention

The corpus intentionally excludes YouTube API results, Reddit, and other source types. GitHub release content and publisher links remain local under ignored `reports/`; repository-authored controls can be shared under the project's license. Live source payloads and model results are not committed by default. This exporter neither checks repository visibility nor grants redistribution rights.

Each manifest entry records `evidence_status`, a content hash, and an optional timezone-aware `expires_at`. A null expiry means no deadline was configured, not unlimited permission to retain content. Set an earlier deadline if the source terms or your authorization require one. On the next CLI `run` or `report`, any expired entry triggers conservative purging of the entire pilot's evidence, responses, and review payloads. There is no background expiry service; schedule cleanup or run it manually when required.

To remove a pilot's retained content immediately:

```bash
uv run python experiments/release_extraction/runner.py purge \
  --workdir reports/experiments/my-pilot
```

The manifest's identifiers/hashes, experiment settings, and aggregate metrics remain. Reports disclose unavailable evidence; they do not recreate or claim to replay purged notes. Deleting a source file yourself also makes it unavailable for replay, but use `purge` to remove derived quotations and review copies too. Save any manual source-derived summary as `findings.md` inside the work directory so it is also purged; independently created copies require their own cleanup.

## Stopping criteria

Finish the pilot when its bounded cases have completed or been explicitly marked failed, pending, or unavailable, and the comparison can be inspected. Keep failures in the denominator. Do not automatically change ranking, enable scheduled model usage, expand source providers, or raise the spend cap to obtain a favorable result.

Consider another shadow round only if human review finds useful, supported changes beyond the complete-input rules. Change default behavior only after testing on held-out releases and confirming that improved coverage does not come with unacceptable unsupported claims, lost caveats, latency, or cost. This pilot cannot establish discovery precision or an X-hours-earlier advantage.
