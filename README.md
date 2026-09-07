# AI Trend Radar

[![CI](https://github.com/dharmendrathinks/ai-trend-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/dharmendrathinks/ai-trend-radar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/downloads/)

A local CLI for discovering promising AI and developer video topics from upstream releases and developer activity.

`ai-trend-radar` watches upstream ecosystem events, ranks the ones worth investigating, and attaches recent YouTube evidence for manual coverage review. It is built for technical creators and researchers deciding what to investigate or cover next, including projects outside their watchlists. It is deterministic, runs without an LLM, and does **not** claim to predict virality or demonstrate a measured early-detection advantage.

[Quick start](#quick-start) · [Slack setup](#optional-slack-delivery) · [Scheduling](#scheduled-runs) · [Configuration](#configuration) · [Troubleshooting](#troubleshooting) · [Contributing](CONTRIBUTING.md)

**Status:** alpha. This README describes `main`, including optional Slack delivery added after [v0.1.0](https://github.com/dharmendrathinks/ai-trend-radar/releases/tag/v0.1.0). For that release's exact scope, see [RELEASE_NOTES.md](RELEASE_NOTES.md). [plan_v2.md](plan_v2.md) is a strategic review and roadmap, not a list of shipped capabilities.

Previously named **YouTube Trend Radar**. Current source installs the `ai-trend-radar` command and `ai_trend_radar` Python package; the old command and import names are no longer provided. For an existing checkout, update `origin` to `git@github.com:dharmendrathinks/ai-trend-radar.git`, then run `uv sync --locked --extra dev` and update scheduled commands. You can keep your checkout's existing folder name and reuse its configuration, databases, and reports. Published v0.1.0 artifacts retain the original names.

```text
Official releases/changelogs + GitHub watchlist/exploration
                         + Hacker News + Hugging Face
                                      ↓
       normalization + freshness + evidence + observed interest
                                      ↓
                             Top Opportunities
                                      ↓
                  YouTube evidence for manual inspection
```

YouTube evidence never affects Discovery Priority. The radar does not calculate a default YouTube crowding or opportunity score.

## Output preview

Illustrative summary with fictional projects and scores, not a live scan or an exact rendering:

```text
Top Opportunities — 2 found

1. Example Coding Agent: background task execution
   Priority: 89.2 · Interest: strong

2. Example Local Runtime: tool-calling support
   Priority: 68.5 · Interest: strong

Release Watch: 5
Community Watch: 2
```

[See how scoring works ↓](#discovery-priority)

## Why this exists

Most trend tools become useful after attention has already accumulated. For a creator covering coding agents, AI IDEs, MCP, local AI, developer models, SDKs, and open-source tools, that can be too late.

This project starts farther upstream. A new official release can matter before it trends; early GitHub, Hacker News, or Hugging Face activity can strengthen the case; YouTube then helps the operator inspect whether the exact viewer intent is already being served.

## Quick start

Requirements: Git, Python 3.12 or newer, [`uv`](https://docs.astral.sh/uv/), and internet access for live collection. Discovery can run without API credentials; a GitHub token is recommended for higher request limits.

```bash
git clone https://github.com/dharmendrathinks/ai-trend-radar.git
cd ai-trend-radar

cp config.example.toml config.toml
cp .env.example .env

uv sync --locked
```

These copy commands are for a fresh checkout. Preserve an existing `.env` and `config.toml` when updating. Add any optional [credentials](#credentials) to `.env`, then run:

```bash
# Check connectivity and configuration; missing optional keys can produce warnings.
uv run ai-trend-radar doctor

# First scan: upstream discovery plus manual YouTube search links.
uv run ai-trend-radar scan --no-youtube
```

To fetch YouTube video metadata, set `YOUTUBE_API_KEY` and run `scan` without `--no-youtube`. A usable scan can still be marked `partial` if a provider is unavailable; inspect the report's provider status. Fewer than ten recommendations, or none, is a valid result.

Useful variants:

```bash
# Run discovery without YouTube API requests.
uv run ai-trend-radar scan --no-youtube

# Request at most five Top Opportunities. The floor may return fewer.
uv run ai-trend-radar scan --top 5

# Use a configuration outside the repository root.
uv run ai-trend-radar scan --config path/to/config.toml
```

Successful scans write these files by default:

| File | Use |
|---|---|
| `reports/latest.brief.md` / `.json` | New discoveries, material updates, and due reminders from the latest scan |
| `reports/latest.md` / `.json` | Full latest report, including Release Watch, Community Watch, and provider status |
| `reports/scan-*.md` / `.json` | Uniquely named full reports from individual scans |
| `data/radar.sqlite3` | Observations, cache, event history, growth checkpoints, and review decisions |
| `data/radar.slack.sqlite3` | Slack delivery outbox and receipts, created only when Slack is used |

Open `reports/latest.brief.md` after the first scan. Runtime output, `.env`, and `config.toml` are ignored by Git at their default locations. Custom output paths outside those ignored locations need their own exclusions.

### Command reference

Prefix each command with `uv run ai-trend-radar`:

| Command | Behavior |
|---|---|
| `scan [--top N] [--no-youtube] [--slack]` | Fetch evidence, rank topics, save reports, and optionally send a Slack brief |
| `brief [--top N]` | Print pending cards from stored scan state and mark those cards presented; no source fetch |
| `decide EVENT_ID reviewed` | Mark one event reviewed |
| `decide EVENT_ID deferred --until TIMESTAMP` | Suppress one event until a future timestamp with a timezone |
| `decide EVENT_ID reopen` | Bring an event back into the pending brief when eligible |
| `feedback EVENT_ID investigate\|brief\|skip --known yes\|no\|unknown` | Record usefulness and whether the development was already known; leaves review/defer state unchanged |
| `feedback-summary [--json]` | Show local feedback counts, unrated presentations, and source/discovery-origin breakdowns in JSON |
| `notify` | Send the latest saved brief and retry queued Slack messages; no source fetch |
| `doctor` | Check configuration, initialize/check storage, and probe source connectivity |

All commands accept `--config PATH`. Decisions also accept `--note TEXT`. Put the global `--verbose` flag before the command, for example `uv run ai-trend-radar --verbose scan --no-youtube`. Use `--help` on any command for its options.


### Changes-only brief and review decisions

Scans also write `reports/latest.brief.md` and `reports/latest.brief.json` alongside the full reports. The brief shows new qualifying discoveries, material updates, and deferred items that are due. Updates and reminders have separate limits, so they do not consume the new-discovery allowance. Exploration continues across the same sources, including projects outside your watchlist.

Use the event ID printed in the report (or an unambiguous prefix of at least four characters):

```bash
# Mark this occurrence reviewed; this does not suppress future releases.
uv run ai-trend-radar decide EVENT_ID reviewed

# Keep it quiet until a specific time, including timezone.
uv run ai-trend-radar decide EVENT_ID deferred --until 2026-10-01T09:00:00+05:30

# Explicitly bring an item back.
uv run ai-trend-radar decide EVENT_ID reopen

# Show remaining pending discoveries and due reminders without fetching.
uv run ai-trend-radar brief --top 5
```

Replace `EVENT_ID` with an ID from your report and choose a future deferral deadline; the timestamp above is an example. `brief` prints pending cards and records their presentation. To reread the brief already presented by a scan, open `latest.brief.md`. Rendering a card is not a reviewed decision. Overflow stays pending rather than being marked presented, and failed output can be retried. This local presentation state is separate from Slack delivery receipts.

### Research feedback

Tell Radar whether a development deserves investigation, a brief mention, or a skip. Record prior awareness separately: something can be useful even if you already knew about it.

```bash
uv run ai-trend-radar feedback EVENT_ID investigate --known no
uv run ai-trend-radar feedback EVENT_ID brief --known yes --note "Useful update, already read the announcement"
uv run ai-trend-radar feedback EVENT_ID skip --known unknown --note "Routine maintenance"
uv run ai-trend-radar feedback-summary
uv run ai-trend-radar feedback-summary --json
```

These commands work offline and accept `--config`. Missing `--known` stays `unknown`; Radar never assumes a topic was new to you. Feedback applies to the event's current revision. Commands embedded in the local brief include `--revision` to reject ratings after the evidence changes. Repeating feedback corrects the latest judgment while retaining its history. Feedback neither changes ranking nor marks an item reviewed; use `decide` for inbox state.

The summary counts investigate/brief/skip, useful previously unknown events, and rated versus unrated brief presentations. It distinguishes unique events from revisions, so updates do not become extra new discoveries. JSON also groups judgments by source family and discovery origin, including `established_repository_search`. These overlapping, self-selected counts are not precision, recall, or proof that a source caused a discovery. Presentation tracking starts with database schema 2 and records brief output, not confirmed reading or Slack delivery. Legacy presentation history is not fabricated.

Reviewed items can reappear when source text changes (whitespace-only changes are ignored), event interest moves into a stronger band, or a watch item qualifies for the main list. These are deterministic triggers, not semantic novelty judgments. Deferred items stay quiet until their deadline even if evidence changes. An overdue item absent from the latest scan is labeled as not rechecked. The full discovery report continues to show current opportunities regardless of review state.

### Optional Slack delivery

Slack delivery is optional and requires no hosted Radar service. Each user connects their own Slack workspace:

1. Create a channel for reports, such as `radar-daily`. You must belong to it if it is private.
2. Open [Slack Apps](https://api.slack.com/apps), create a **Blank app** (or **From scratch**), name it, and select your workspace.
3. In the app settings, open **Incoming Webhooks** and turn **Activate Incoming Webhooks** on.
4. Click **Add New Webhook to Workspace**, choose your channel, and authorize it.
5. Copy the generated URL into your local `.env` as `SLACK_WEBHOOK_URL=YOUR_REAL_WEBHOOK_URL`, preserving your other settings.

See [Slack's setup guide](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/) for details. The CLI loads `.env` from its working directory; keep the URL out of TOML, command arguments, Git, and shared logs.

```bash
# Collect and save reports, then send the changes brief to your Slack channel.
uv run ai-trend-radar scan --slack

# Send the latest saved brief and retry queued messages without another scan.
uv run ai-trend-radar notify
```

Both commands accept `--config`. Normal `scan` and `brief` commands never send Slack messages, even if the webhook is configured. For scheduled delivery, add `--slack` to your scheduler's scan command and set its working directory to the folder containing `.env`. A local schedule requires the machine to be available and online.

Slack gets a compact changes brief with event IDs, source links, collection gaps, and overdue-item caveats. Empty briefs explicitly say there are no new qualifying changes. Messages show at most 14 new discoveries, three updates, and three reminders, with an explicit notice when more cards are available locally. Full reports, release notes, and YouTube results are not uploaded. Review/defer decisions remain local CLI commands.

Delivery state lives in a separate SQLite file next to the configured database (`data/radar.slack.sqlite3` by default). It does not change the radar database schema. Each scan's message is queued before local acknowledgement; a failed send survives later scans and can be retried with `notify` or the next `scan --slack`. Network/server errors get up to three attempts; rate limits retain Slack's retry deadline. Confirmed sends are not resent for the same scan and webhook. Timeouts or a crash between Slack accepting a message and the local receipt can still cause duplicates; exactly-once delivery is not guaranteed.

Only a hash of the webhook is stored in the outbox. Pending messages contain the compact brief; successful delivery clears the payload field and retains a receipt. Changing the webhook creates a separate destination: pending messages for the old URL are not automatically forwarded to the new channel. Up to ten queued briefs are sent per invocation, oldest first. `notify` sends the saved scan brief; it does not create a new brief from subsequent review decisions.

### Scheduled runs

The CLI runs once and exits. Installing the project does not create a schedule. Use your own scheduler, such as a macOS LaunchAgent, Linux cron/systemd timer, or Windows Task Scheduler.

For a daily briefing, configure one run before your normal research time:

| Scheduler setting | Value |
|---|---|
| Executable | Absolute path to your `uv` executable |
| Arguments | `run --locked ai-trend-radar scan --slack --config config.toml` |
| Working directory | Absolute path to the checkout containing `.env` and `config.toml` |
| Time | Your preferred daily time, with the scheduler's timezone set explicitly |
| Output | A local log file for stdout and stderr |

Omit `--slack` for local reports only; add `--no-youtube` to skip video metadata. The machine must be available, with internet access and any login session required by the scheduler. Check one manual run before enabling the schedule. Scheduler configuration and logs belong to the operator, not in the public repository.

Reuse the same database across runs and avoid overlapping scans against it. Fresh temporary runners lose observed-growth baselines, review decisions, and Slack receipts unless you persist the state. The included [GitHub Actions workflow](.github/workflows/ci.yml) runs tests and builds; it does not collect daily reports.

### Event and input guarantees

- Distinct releases have distinct event IDs; adding exact supporting evidence preserves the stored ID. Native GitHub release IDs distinguish recreated releases, and conflicting releases cannot be combined through a third item.
- Repository star-growth events use successive persisted checkpoints and fixed observation intervals. Unchanged counters do not create a new occurrence or reset freshness. A long observation interval is reported as such, not described as acceleration.
- Repository-wide activity and discussions linking to a project homepage are project context, not interest in a specific release.
- Cross-source coverage is a ranking heuristic, not independent verification of a capability.
- Undated items age from their stored first-seen time. JSON distinguishes acquisition, validation, cache state, and event-time basis. Cache/stale reads do not create metric observations; a 304 records an explicit revalidation while preserving body acquisition time.
- GitHub release Markdown is captured separately from the 2,000-character display summary. The deterministic extractor reads the captured notes, so features near the end can be found. Official feeds retain supplied full-content fields or mark summary-only content. Documents are capped at 1,000,000 characters and incompleteness is disclosed. Collection limits still apply; no linked-page crawler or broad pagination was added.

Full source text is local data in SQLite and JSON reports, just like the existing source summaries. It is not included in Git. This change does not establish unrestricted redistribution rights for third-party content or an exact historical replay guarantee.

### Existing databases

The application applies an additive, idempotent SQLite migration on the next run. Existing source items, scans, and metric history are retained. Old metric observations lack cache provenance, so they are labeled `legacy` and excluded from new measured-growth baselines. New growth checkpoints begin with a verified response; do not interpret that initial warm-up as a lack of project activity.

Old reports keep their original identities. New reports use schema `2.0`, corrected scoring `v1.1`, and deterministic extraction `release-topic-v1.2`. `event_id` is the durable review key; `fingerprint` remains an alias for compatibility. Review state starts with the new event ledger. Unsupported future database versions are rejected.

### Updating and keeping state

For an existing `main` checkout with your work committed or otherwise preserved:

```bash
git pull --ff-only
uv sync --locked
```

Compare new settings in `config.example.toml` and `.env.example` with your local files; do not overwrite your credentials or watchlist. The repository workflow uses the committed dependency lockfile ([uv's locking behavior](https://docs.astral.sh/uv/concepts/projects/sync/)).

Pause scheduled runs before backup or upgrade. Back up your configured database directory while no Radar process is using it, including SQLite sidecar files and the Slack outbox if present; keep a private copy of your configuration separately. Deleting the radar database resets observed history and decisions. Deleting Slack receipts can cause a previously sent brief to be sent again. There is no historical backfill or downgrade migration command.

The discovery/feedback update adds three SQLite tables and upgrades the database to schema **2**, preserving existing events, observations, and decisions. Older code that supports only schema 1 refuses the upgraded database. Restore a pre-upgrade backup if reverting to that code. New GitHub collection settings are opt-in for existing configurations; copy the desired `established_*` keys from the example. Feedback needs no additional configuration.

For the published snapshot, see the [v0.1.0 release](https://github.com/dharmendrathinks/ai-trend-radar/releases/tag/v0.1.0), which includes a wheel, source distribution, and checksums. The wheel installs the CLI; example configuration comes from the repository or source distribution. That release was not published to PyPI.


## What it watches

| Source | Responsibility | Credential |
|---|---|---|
| Official RSS/Atom feeds | Product releases, changelogs, and authoritative announcements | None |
| GitHub watched repositories | Releases plus repeated aggregate repository observations | `GITHUB_TOKEN` optional, recommended |
| GitHub exploration | New repositories plus optional discovery and measured follow-up of older active projects | `GITHUB_TOKEN` optional, recommended |
| Hacker News | Relevant submissions, points, comments, and observed change | None |
| Hugging Face | Emerging models and Spaces with supported public metadata | `HF_TOKEN` optional |
| YouTube | Recent video metadata and direct searches for manual coverage inspection | `YOUTUBE_API_KEY` optional |

Providers are isolated: one unavailable provider does not terminate an otherwise usable scan. Reports identify failures, stale/cache state, and missing evidence.

## Output model

- **Top Opportunities** — actionable topics worth investigating now. The configured count is a maximum; the radar returns fewer results rather than backfilling weak candidates.
- **Release Watch** — release or authoritative changelog events that lack a sufficiently useful/current video angle or do not meet the main-list presentation floor.
- **Community Watch** — relevant discoveries retained outside the primary list because of weak or stagnant evidence, English-orientation gates, freshness, or insufficient promotion evidence.

Each recommendation includes timestamps, score inputs, triggering rules, observed signals, missing evidence, source links, and YouTube evidence when available. Markdown is designed for reading; JSON is suitable for downstream tooling.

Release cards can include a primary video angle, alternatives, and supporting release-note text extracted by deterministic rules. Low-specificity releases stay in Release Watch. These are research suggestions for human review, not model-generated scripts.

## Discovery Priority

Freshness uses the best credible event timestamp and a configurable 48-hour half-life:

```text
Freshness = 100 × 2 ^ (-age_hours / 48)
```

Evidence Strength reflects observable provenance: an authoritative source, cross-source coverage, or a single community source. Interest is a configured `strong`, `moderate`, or `early/limited` band backed by current HN, GitHub, Hugging Face, and source-family measurements.

```text
Discovery Priority = 0.60 × Freshness
                   + 0.25 × Evidence Strength
                   + 0.15 × Interest Value
```

Discovery Priority orders discovery evidence; it is not a probability, virality forecast, or YouTube opportunity score. A separate presentation floor requires sufficient freshness plus moderate/strong interest, cross-source coverage, or authoritative actionable evidence before a candidate enters Top Opportunities. All thresholds live in `config.toml` and are starting heuristics, not scientifically calibrated predictions.

## Credentials

Copy `.env.example` to `.env` and set only the credentials you want to use:

```dotenv
GITHUB_TOKEN=
HF_TOKEN=
YOUTUBE_API_KEY=
SLACK_WEBHOOK_URL=
```

- **`GITHUB_TOKEN`** — optional but strongly recommended. Without it, GitHub uses anonymous public API access with substantially lower rate limits; GitHub providers degrade independently if that quota is exhausted.
- **`HF_TOKEN`** — optional for public models and Spaces. It can improve authenticated access but is not required for normal public discovery.
- **`YOUTUBE_API_KEY`** — optional. Without it, discovery and ranking still work and reports provide manual YouTube search links, but no live YouTube video metadata is retrieved.
- **`SLACK_WEBHOOK_URL`** — optional; used only by `scan --slack` and `notify`. Follow [Slack setup](#optional-slack-delivery). It is a secret, even though it looks like a URL.

The CLI reads `.env` from the current working directory, not from the directory selected by `--config`. Existing environment variables take precedence over `.env`; GitHub also accepts `GH_TOKEN` as a fallback. `doctor` checks source access but does not validate or post to Slack.

Never commit `.env`, credentials, private reports, or local databases. The CLI suppresses verbose HTTP logging that could otherwise expose query-string credentials, and cached URLs redact sensitive parameters.

## First run and repeated runs

The radar never invents historical momentum.

On a repository or story's first observation, the report shows current aggregates and explicitly marks observed growth as unavailable. Repeated scans allow SQLite to measure changes since tracking began, including:

- GitHub stars at first observation, current stars, observed delta, and duration.
- Hacker News point/comment change over the observation window.
- Hugging Face metric changes where supported.

These are **observed changes while your radar was running**, not reconstructed historical growth.

Watched-repository growth events also require the configured minimum interval and growth thresholds (by default, at least 24 hours, 50 stars, and 0.5% growth). Two closely spaced scans do not guarantee a growth event. Keep scanning the same database; cached or stale reads do not add a new metric measurement.

## YouTube evidence and limitations

For promoted candidates within the configured YouTube candidate limit, the default setup generates up to two compact event-specific searches and retrieves supported recent video metadata. Search requests use `type=video`, relevance ordering, an English relevance-language preference, and a configurable publication window.

YouTube search can still return noisy or loosely related videos. V1 preserves YouTube-returned content and order for manual inspection and deliberately avoids an expanding list of negative keywords. It does not silently filter results or calculate a default relevance ratio, crowding score, views-per-hour metric, creator tier, or YouTube-derived Opportunity Score.

Optional deterministic title/channel annotations exist behind `youtube.enable_local_relevance_annotations`, disabled by default. Enable them only after reviewing and complying with YouTube's applicable derived-metrics terms. See the [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies), [`search.list` documentation](https://developers.google.com/youtube/v3/docs/search/list), and [derived metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy).

**Retention:** live-only YouTube handling and automatic source-specific expiry cleanup are not implemented. API responses can remain in the HTTP cache, database scan records, and generated reports; cache expiry is not deletion. Manage stored data according to source requirements. Reports are evidence snapshots, not a guarantee of exact replay, and source access does not grant unrestricted redistribution rights.

## Configuration

`config.example.toml` is a runnable, credential-free starting point. Copy it to `config.toml` before running the CLI. It controls:

- Official feeds, watched repositories, and GitHub exploration queries.
- Provider result bounds, lookback windows, caching, and retries.
- Entity aliases and developer-channel relevance terms.
- Deduplication anchors and release-topic extraction terms.
- Eligibility, interest, English-orientation, stagnation, and main-list thresholds.
- Ranking weights and YouTube request budgets.

Thresholds are deliberately external to code so real scan results can inform later tuning. Reports persist the effective values and configuration fingerprint.

Start with these settings before changing scoring rules:

| Setting | What to change |
|---|---|
| `github.watched_repositories` | Add `owner/repository` names for known projects you want to follow |
| `github.exploration_queries` | Broaden discovery beyond the watchlist; `{since}` is replaced from the scan lookback |
| `github.established_queries` | Search older recently active projects for bounded follow-up; empty or missing disables this lane |
| `github.established_tracking_limit`, `established_followup_per_scan`, `established_tracking_days` | Bound active projects, follow-up requests per scan, and each observation window |
| `official.feeds` | Add RSS/Atom feeds with a name and URL; an entity label is optional |
| `relevance.*` | Adjust the AI/developer vocabulary used to select relevant items |
| `scan.lookback_days`, `scan.top_results` | Change the event window and maximum number of opportunities |
| `youtube.enabled`, `youtube.request_budget` | Disable video metadata or bound per-scan searches; the search budget is not an API quota-unit budget |

The original exploration queries keep their own result allowance and star ordering. The example also searches two topics for older, recently pushed public repositories, sorted by update time. Recent pushes and search position are admission signals, not measured attention. GitHub documents [repository search qualifiers](https://docs.github.com/en/search-github/searching-on-github/searching-for-repositories) and [search limits and incomplete results](https://docs.github.com/en/rest/search/search).

The established-project lane checks relevance, excludes watched/private/archived/forked repositories, and retains at most **20** projects for **14 days** in the example. It takes at most **5** results per search and makes at most **10** follow-up metadata requests per scan, even after a project leaves search results. Failures consume a follow-up turn so one failing project cannot monopolize collection. Fixed windows expire; re-admission resets its growth checkpoint. Search caps and limited topics still mean incomplete coverage.

The first observation only starts a baseline; a repository does not become a fresh event just because Radar found it. Subsequent verified measurements must meet the existing `watched_repo_growth_*` thresholds (used for both watched and discovered repository snapshots) before producing an observed-growth event. Cached/stale samples do not count as new measurements. Unchanged counters do not create another fresh event. Full reports show tracking counts while baselines accumulate. This is observed growth, not a claim of accelerating activity.

Relative `paths.database` and `paths.reports` values resolve from the configuration file's directory. Credentials remain in `.env` or the process environment, not TOML.

## Architecture

The project is one Python package and one CLI:

```text
provider modules (concurrent, failure-isolated)
    → normalized SourceItem records
    → conservative entity resolution and event clustering
    → deterministic scoring, release-topic extraction, and presentation gates
    → optional YouTube evidence
    → full Markdown + JSON reports
    → changes brief from persisted event/review state
    → optional Slack outbox and webhook delivery

Radar SQLite database
    ↳ source observations
    ↳ aggregate-change history
    ↳ HTTP cache
    ↳ event identities, revisions, and review decisions
    ↳ measured-growth checkpoints
    ↳ saved scan records

Separate Slack SQLite database (optional)
    ↳ pending compact briefs and delivery receipts
```

Collection uses external source APIs, but all Radar state runs locally. No hosted Radar backend, message broker, vector database, or mandatory AI API is required. Slack uses a local SQLite outbox rather than a queue service.

## Troubleshooting

| Symptom | Check |
|---|---|
| Configuration not found | Copy `config.example.toml` on first setup, or pass `--config` to an existing file |
| No Top Opportunities | Check provider status, Release Watch, and Community Watch in `latest.md`; the radar never fills the list with weak candidates |
| `brief` is empty after a scan | Open `latest.brief.md`; the scan already marked its cards presented |
| Growth is unavailable | Reuse the same database and wait for verified measurements over a sufficient interval; no history is invented |
| GitHub quota exhausted or scan marked partial | Run `doctor`, configure a GitHub token if needed, and inspect the report's collection gaps |
| YouTube metadata missing | Check the key, `--no-youtube`, `youtube.enabled`, candidate limit, and search budget; manual search links remain available for checked candidates |
| Scheduled run misses credentials or writes elsewhere | Set the working directory explicitly and check the scheduler's executable path, timezone, availability, and logs |
| Slack sends nothing | Use `scan --slack` or `notify`; setting the URL alone does not enable delivery. Check that `.env` contains the real webhook, not the example placeholder |
| `notify` prints `0 sent; 0 pending` | The saved brief may already be delivered, or there is no saved/queued brief. Run a new scan when you want fresh evidence |
| Slack delivery is pending | Check the webhook/channel settings and retry with `notify`; a stored rate-limit deadline must pass first |

On normal CLI paths, `0` means the command succeeded (a scan can still be partial); `1` indicates an application error. `scan --slack` returns `2` if reports were saved but delivery failed or remains pending, and `notify` returns `2` while messages remain queued. Argument parsing also uses `2` for invalid command syntax, so read stderr rather than relying on the number alone.

For a bug report, include the commit/tag, command with secrets removed, and relevant provider statuses. Use [GitHub Issues](https://github.com/dharmendrathinks/ai-trend-radar/issues); do not attach `.env`, webhook URLs, or unreviewed database/report dumps.

## Development

Install the project and credential-free test dependencies:

```bash
uv sync --locked --extra dev
```

Run the checks used for release preparation:

```bash
uv run pytest
uv run ai-trend-radar --help
uv build
```

Tests use fixtures and mocked HTTP responses; they do not require source credentials or a real Slack webhook, and they do not send Slack messages. Run `uv run ai-trend-radar doctor` separately when you want a live connectivity check; it initializes storage and may warn about optional missing credentials. Passing tests establishes implementation behavior, not recommendation precision or early-detection effectiveness.

See [CONTRIBUTING.md](CONTRIBUTING.md) before changing provider behavior, eligibility, or scoring.

## Current limitations

- Deterministic heuristics require calibration against real use; they are not learned predictions.
- No runtime LLM, semantic embedding model, or virality prediction is used. The proposed optional LLM extraction experiment is not implemented yet.
- Growth is measured only after local tracking begins.
- Provider availability, API quotas, upstream schemas, and feed quality constrain results.
- Presentation is English-oriented using a transparent Latin-script proxy, not full language identification.
- Entity resolution and deduplication are intentionally conservative, so occasional duplicates are preferred over incorrect merges.
- YouTube competition remains a manual judgment.
- Optional Slack briefs are the only notification integration. No dashboard, historical backfill, Reddit, X/Twitter, Google Trends, or paid trend provider is included.

## Source APIs and attribution

- [GitHub REST API](https://docs.github.com/en/rest)
- [Hacker News API](https://github.com/HackerNews/API)
- [Hugging Face Hub API](https://huggingface.co/docs/huggingface_hub/package_reference/hf_api)
- [YouTube Data API](https://developers.google.com/youtube/v3)

Individual source names and links are retained in generated reports. Use of every API remains subject to its terms, quotas, attribution requirements, and future policy changes.

## Project policies

- Contributions: [CONTRIBUTING.md](CONTRIBUTING.md)
- Security reports: [SECURITY.md](SECURITY.md)
- License: [MIT](LICENSE)
