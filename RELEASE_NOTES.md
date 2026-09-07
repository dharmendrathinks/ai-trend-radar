# v0.2.0 — AI Trend Radar with optional LLM-discovered updates

Released September 7, 2026. Alpha software; editorial scores require human judgment.

- The project, Python package, CLI, and GitHub repository are now `ai-trend-radar` / `ai_trend_radar`. Existing checkout directory names can stay unchanged. The old command and import names are not retained.
- Normal scans optionally extract capability updates from complete GitHub release notes using the locally authenticated Codex CLI. “LLM-discovered updates” is the first report section, with source links, exact quotes, developer value, caveats, and extraction status.
- LLM enrichment also covers up to five public linked pages from HN stories selected for Top Opportunities, before the release lane under the same model-call cap. It requires page evidence, reports skipped pages, and labels HN submission time separately from product launch time. The reader bounds redirects and content size, validates/pins public addresses, and uses no credentials, cookies, or browser execution.
- Extraction happens before deterministic presentation cutoffs. It does not change deterministic Discovery Priority, review state, YouTube validation, or Slack briefs. Model suggestions still require human review.
- LLM topics are globally ranked and numbered by a separate Overall Priority. Developer impact, demo potential, freshness, and audience impact each have their own /100 score and explanation, with a comparison table in that order. Overall weights are 30%/30%/20%/20%. Freshness is calculated; the other three are LLM editorial judgments. Audience impact assesses relevance, not predicted reach. Missing dates/scores remain unavailable rather than invented. Audience is configurable; these scores do not predict views or demand.
- Model use is opt-in through `scan --llm` or `[llm] enabled = true`; `--no-llm` overrides configuration. Defaults cap releases, calls, input size, and per-call time. Cached successes, abstentions, and failures avoid repeated calls for unchanged notes.
- Adapter/prompt/schema assets ship in the wheel and are shared with the standalone comparison harness. The harness preserves human CSV labels and supports latest-scan corpus selection.
- Full report JSON advances to schema 2.3: schema 2.1 introduced `llm_updates`; 2.2 added `ranked_topics` and `ranking`; 2.3 adds source kinds, time/discovery provenance, and coverage metadata for HN page assessment. Editorial rubric is video-topic-v1. Database schema 2, Discovery Priority scoring v1.1, and deterministic extraction release-topic-v1.2 are unchanged.
- Includes post-v0.1.0 source changes: optional Slack delivery, established-repository tracking, and researcher feedback workflows documented in the README.

## Updating an existing checkout

Preserve `.env`, `config.toml`, databases, and reports. Update the Git remote to `git@github.com:dharmendrathinks/ai-trend-radar.git`, sync with `uv sync --locked --extra dev`, and change scheduled commands to `ai-trend-radar` if still using the old name. No `[llm]` section means no model calls. To opt in, copy only that section from `config.example.toml`; use an absolute `codex_binary` path for schedulers with a minimal PATH.

Enabling model extraction sends configured GitHub release notes and selected HN linked-page text to the model service and consumes the operator's model allowance. Confirm permission, especially for private repositories. Cache data stays local and should not be committed. Failed extractions do not prevent the deterministic report; inspect the separate LLM status. See the README for cache retry and cost bounds.

---

# v0.1.0 — First public release

> Historical release: this release was published as YouTube Trend Radar. The project was subsequently renamed to AI Trend Radar (`ai-trend-radar`, Python package `ai_trend_radar`). The original release artifacts and instructions below retain their original names; use the [README](README.md) for current-source installation and commands.

YouTube Trend Radar is an open-source, local CLI for discovering promising AI and developer-tool topics, inspecting the evidence, and deciding what deserves further research. This first release includes the complete product built so far, including the correctness fixes and changes-only brief.

The radar starts with upstream events and developer activity. YouTube supplies downstream coverage evidence for human inspection. There is no runtime LLM requirement, and YouTube evidence does not affect discovery ranking.

## Discovery sources

- **Official RSS/Atom feeds:** product announcements and changelogs, with configurable feeds and entity aliases.
- **GitHub watchlists:** repository releases and locally observed repository activity.
- **GitHub exploration:** newly created AI/developer repositories outside the watchlist.
- **Hacker News:** relevant submissions, discussion activity, and observed point/comment changes.
- **Hugging Face:** emerging models and Spaces using supported public metadata.
- **Optional YouTube evidence:** event-specific searches, recent video metadata, and manual search links when API access is unavailable.

Collection runs concurrently with bounded requests, HTTP caching, conditional requests, retries, and provider failure isolation. Reports disclose unavailable or stale evidence rather than requiring every provider to succeed.

## Explainable topic selection

- Event normalization, configurable relevance rules, entity aliases, and conservative deduplication.
- Transparent freshness, evidence, and interest inputs with configurable thresholds and ranking weights.
- **Top Opportunities** for candidates that meet the presentation floor. The requested count is a maximum; weak results are not added just to fill it.
- **Release Watch** for releases that need stronger topicability or more suitable timing.
- **Community Watch** for relevant but weak, stagnant, or otherwise gated community discoveries.
- Deterministic release-note extraction with a primary topic angle, alternatives, supporting evidence, and event-specific search queries.
- Full returned GitHub release Markdown captured separately from the short display summary and used by the extractor. Official feed content fields are retained when supplied; summary-only or capped inputs are identified.

## Correctness and evidence guarantees

- Durable occurrence IDs distinguish releases, remain stable across title/body edits, and preserve review state when exact supporting evidence arrives.
- Conflicting releases cannot be merged through a shared title or an intermediate source. Native GitHub release IDs distinguish a recreated release from a renamed tag on the same release.
- Repository growth events use persisted checkpoints and fixed observation intervals. Unchanged counters cannot repeatedly become fresh growth events.
- Growth is observed only after tracking begins; the radar does not invent historical momentum or claim acceleration from cumulative totals.
- Repository-wide star growth and discussions linking to a project homepage are project context, not interest in a specific release.
- Cross-source coverage is described as coverage, not independent verification of a capability.
- Undated items age from their persisted first-seen time.
- Item provenance distinguishes acquisition, server validation, and cache state. Cached/stale reads do not create new metric measurements; a valid 304 records revalidation while preserving body-acquisition time.

## Changes-only brief and decisions

Each scan writes the full Markdown/JSON reports and `latest.brief.md` / `latest.brief.json`.

The brief separates:

1. New qualifying discoveries, including unfamiliar projects.
2. Material updates to previously presented events.
3. Deferred items whose deadline has arrived.

Updates and reminders have separate limits, preserving the new-discovery allowance. Overflow stays pending. Writing a brief does not mark an event reviewed, and failed brief output can be retried without consuming pending cards.

Use the event ID shown in a report:

```bash
uv run youtube-trend-radar decide EVENT_ID reviewed
uv run youtube-trend-radar decide EVENT_ID deferred --until 2026-10-01T09:00:00+05:30
uv run youtube-trend-radar decide EVENT_ID reopen
uv run youtube-trend-radar brief --top 5
```

Reviewed items can resurface after source-text changes, stronger event interest, or promotion from a watch list. Deferred items stay quiet until their deadline. Decisions on one release do not suppress future releases, and review state does not disable discovery collection.

`brief` prints remaining pending cards without fetching. To reread the brief already delivered by a scan, open `latest.brief.md`. All new commands support `--config`; decisions can include `--note`.

## Local operation and installation

Requirements: Python 3.12+ and `uv` for the repository workflow.

```bash
git clone https://github.com/dharmendrathinks/youtube-trend-radar.git
cd youtube-trend-radar
git checkout v0.1.0
cp config.example.toml config.toml
cp .env.example .env
uv sync --locked
uv run youtube-trend-radar doctor
uv run youtube-trend-radar scan
```

To scan without YouTube API requests:

```bash
uv run youtube-trend-radar scan --no-youtube
```

GitHub and Hugging Face credentials are optional. YouTube API metadata requires `YOUTUBE_API_KEY`; manual search links remain available without it. Configuration controls sources, watchlists, relevance, collection bounds, thresholds, ranking weights, cache behavior, and YouTube request budgets.

The GitHub release includes a Python wheel, source distribution, and SHA-256 checksums. The wheel installs the CLI; use the repository or source distribution for the example configuration and documentation. This release does not publish the package to PyPI.

## Persistence and compatibility

- SQLite stores source records, metric observations, HTTP cache, scan reports, event identities, growth checkpoints, and review decisions.
- Generated reports, databases, credentials, and local configuration are ignored by Git.
- Existing development databases receive an additive, idempotent migration. Old metric observations remain available as legacy history but are excluded from new growth baselines because their cache provenance is unknown.
- Growth checkpoints start with newly verified measurements. Existing report identities are not retroactively rewritten.
- New JSON reports use schema **2.0**, corrected scoring **v1.1**, and extraction **release-topic-v1.2**. `event_id` is the durable review key; `fingerprint` remains an alias in new reports.
- Unsupported future database schemas are rejected.

## Validation

Release preparation passed **92 tests** covering provider behavior, ranking, extraction, identity conflicts, cache semantics, growth intervals, migrations, review/defer behavior, brief overflow, and failed-output recovery. Both the wheel and source distribution build successfully. Tests use fixtures and mocked providers; they do not establish live detection precision or an early-discovery advantage.

## Boundaries and known limitations

This is an initial, alpha-stage release. Ranking thresholds are deterministic heuristics, not calibrated probabilities or virality predictions. Provider quotas, result caps, source quality, and an English-oriented presentation gate constrain discovery. Complete captured text does not imply complete provider coverage; there is no linked-page crawler or broad historical backfill.

YouTube competition remains a human judgment. Optional local relevance annotations are disabled by default and subject to the applicable API terms. The separately discussed live-only YouTube retention change is not included in this release: existing API cache/report persistence remains, without a comprehensive expiry cleanup system. Operators must manage retained API data according to source requirements. Source access does not grant unrestricted redistribution rights.

There is no LLM extraction experiment, prospective effectiveness study, story graph, semantic clustering, dashboard, hosted service, alert delivery, or broad new provider integration in this release. Exact historical replay is not guaranteed.

The repository includes `plan_v2.md`, the strategic review of the pre-implementation repository and longer-term possibilities. Its findings describe that review snapshot; its roadmap is not a claim that those capabilities have shipped.

## Project

- MIT licensed.
- Contribution guidance: [CONTRIBUTING.md](CONTRIBUTING.md).
- Security reporting: [SECURITY.md](SECURITY.md).
- Setup, scoring, configuration, and operation: [README.md](README.md).
