# AI Trend Radar

[![CI](https://github.com/dharmendrathinks/ai-trend-radar/actions/workflows/ci.yml/badge.svg)](https://github.com/dharmendrathinks/ai-trend-radar/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A local CLI for understanding AI changes that affect developers.

> What changed, which developers should care, and what difference could it make to their work?

Radar watches official feeds, GitHub, Hacker News and Hugging Face. It surfaces
capabilities, consequential fixes, breaking changes, pricing/access updates and
practical findings for developers using AI or building AI-powered software.
YouTube is an optional appendix, not the purpose or ranking criterion.

**Status: alpha, developer-first release
[v0.3.0](https://github.com/dharmendrathinks/ai-trend-radar/releases/tag/v0.3.0).**
This release replaces v0.2.0's separate LLM report and video-oriented rubric.
Report/brief JSON and database schemas advance to version 3; read the
[upgrade notes](RELEASE_NOTES.md#upgrading-from-v020) before adopting it.

[Quick start](#quick-start) · [Scoring](#developer-priority) ·
[Configuration](#configuration) · [Slack](#optional-slack-delivery) ·
[Plans and status](docs/README.md) · [Release notes](RELEASE_NOTES.md)

The repository/package/command remain `ai-trend-radar` / `ai_trend_radar`.
Existing checkout folder names, including `youtube-trend-radar`, need not change.

## Quick start

Requirements: Python 3.12+, Git, uv and internet access for collection.
The automated tests require no API credentials.

```bash
git clone https://github.com/dharmendrathinks/ai-trend-radar.git
cd ai-trend-radar
cp config.example.toml config.toml
cp .env.example .env
uv sync --locked --extra dev

# Source-grounded discovery, without model calls or YouTube requests.
uv run ai-trend-radar scan --no-llm --no-youtube

# Richer assessments using your local Codex login/model allowance.
uv run ai-trend-radar scan --llm --no-youtube
```

Copy examples only for a fresh installation. Preserve existing credentials,
watchlists and configuration. LLM use stays opt-in; missing model access produces
a usable report with unassessed entries, not fabricated scores. Collection or
assessment gaps can make a usable scan partial.

Open `reports/latest.md` for the full numbered report or
`reports/latest.brief.md` for new/changed/due developments. JSON companions and
uniquely named scan reports are also saved. Runtime state stays out of Git at
the default paths.

## Developer Priority

Each assessed development has its own /100 scores and reasons:

| Category | Weight | Question |
|---|---:|---|
| Developer impact | 50% | How substantially does this affect the relevant workflow? |
| Developer relevance | 30% | How directly does this matter to AI-using/building developers? |
| Urgency | 20% | How promptly should affected developers investigate or respond? |

Overall is calculated in code to one decimal. The versioned rubric is
**developer-priority-v1**, with 0/25/50/75/100 anchors. These are editorial judgments,
not measured productivity, adoption, demand or video-success predictions.

Urgency describes circumstances at assessment time—not post age or a live deadline
countdown. Age and evidence type are separate. A recent HN post does not prove a
new launch; a modified model card does not prove a new capability.

Main-list floors: overall 50, impact 25 and relevance 50. Lower-scoring developments
go to Watch. Scored entries sort by overall, impact, relevance, urgency, event time
and stable topic ID. Unassessed discoveries follow with **N/A** in deterministic
Discovery Priority order. N/A means unknown, not low usefulness.

`--top N` limits the combined main list, not assessment. Fewer results are valid.
Multiple changes in one release receive separate entries and source links; the
generic event entry is replaced once its developments are extracted.

### Deterministic Discovery Priority

The existing **v1.1** baseline remains a collection/fallback ordering aid:
60% freshness, 25% evidence strength and 15% observed interest. These configurable
weights are not the developer-impact rubric. Stars, discussions and source coverage
are attention/provenance signals, not verified usefulness.

Meaningful fixes remain eligible under maintenance headings. Assessment happens
before presentation cutoffs, without requiring HN popularity or a short freshness
floor. Relevance filtering and the configured lookback still bound discovery.

## Commands and individual-topic review

Prefix commands with `uv run ai-trend-radar`:

| Command | Behavior |
|---|---|
| `scan [--top N] [--llm\|--no-llm] [--youtube\|--no-youtube] [--slack]` | Collect, assess, rank and save; Slack only with explicit opt-in |
| `brief [--top N]` | Print pending new/updated/due topics offline, recording presentation |
| `decide TOPIC_ID reviewed` | Review this development, not its siblings |
| `decide TOPIC_ID deferred --until TIMESTAMP` | Defer one topic until a future timezone-aware timestamp |
| `decide TOPIC_ID reopen` | Reopen a topic |
| `feedback TOPIC_ID investigate\|brief\|skip --known yes\|no\|unknown` | Record usefulness and prior awareness without changing review state |
| `feedback-summary [--json]` | Topic feedback plus separately identified legacy event history |
| `notify` | Send saved Slack brief/retry outbox; no source fetch |
| `doctor` | Check configuration, storage and provider connectivity |

All commands accept `--config PATH`. Decisions/feedback accept notes. Feedback
accepts `--revision N` to reject ratings for superseded evidence. Use `--event`
explicitly for legacy event-level decisions or feedback.

Topic identity follows source anchors, not generated titles or scores.
Ambiguous relationships remain separate rather than silently sharing review state.
Model rewording or scores alone do not create a discovery. Material source changes
may revise a topic or create a related topic when its anchor cannot be matched.

The brief uses the same topics/order as the full report, with separate new,
updated and due allowances. Overflow stays pending; presentation is not review.
Deferred topics stay quiet until due. Feedback does not train weights or
automatically change eligibility.

## Optional Slack delivery

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

## Scheduled runs

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

## Existing databases and migration

The first source-checkout command that initializes storage upgrades to SQLite
schema **3**. Before upgrading an existing database, Radar creates a SQLite backup
beside it named `<database>.pre-v3.bak`; committed WAL contents are included.
Keep the backup private. Do not overlap scans or migration against the same state.

Observations, legacy events, decisions, feedback and outbox history are preserved.
The initial extracted topics inherit a current legacy reviewed/deferred state,
explicitly labeled inherited. Legacy usefulness ratings are not copied into
individual-topic judgments. Later new developments are independently reviewable.

New full/brief JSON uses schema **3.0**. Entries contain `topic_id`, parent event
links, evidence, assessment status, `developer_priority`, revision and review state.
Old `llm_updates` and creator-specific fields are not emitted by new scans.
Historical reports stay unchanged. Legacy rendering and extraction-only experiment
support remain available.

Older binaries cannot use schema 3. To roll back, stop overlapping processes and
restore the backup to a separate configuration/database path; new topic decisions
made after migration are not present in that backup.

## Configuration

See [config.example.toml](config.example.toml). Preserve your `.env` and local config.

- `[developer] audience`: up to 2,000 characters describing developers served.
  Defaults to developers using AI and building AI-powered software. Legacy
  `llm.audience` is a deprecated fallback; the new setting takes precedence.
- `[llm] enabled`: false by default; CLI overrides apply to one scan.
- Model, effort and executable remain configurable. The adapter uses local Codex
  authentication, not a newly required API key. Use an absolute executable path
  for schedulers with a minimal PATH.
- Default bounds: 40 candidate events, 12 additional documents, 24 new model calls,
  20 GitHub releases, five HN candidates, 20,000 source-text characters per event
  bundle and 120 seconds per call. Zero HN/page limits disable those reads.
  At most three developments are extracted per event.
- Source-family round-robin assessment uses discovery order within each family.
  Existing complete supporting text is bundled if it fits; no independent research
  crawl is performed.
- `[youtube] enabled`: false by default; existing explicit opt-ins are respected.
  `scan --youtube` enables an appendix; `--no-youtube` suppresses it.
- Collection/watchlist/exploration, interest, lookback, HTTP, cache and storage
  settings remain configurable. No new discovery provider is added.

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
- **`YOUTUBE_API_KEY`** — optional. When the appendix is enabled, missing credentials produce manual search links instead of live video metadata. No YouTube requests are made by a default scan.
- **`SLACK_WEBHOOK_URL`** — optional; used only by `scan --slack` and `notify`. Follow [Slack setup](#optional-slack-delivery). It is a secret, even though it looks like a URL.

The CLI reads `.env` from the current working directory, not from the directory selected by `--config`. Existing environment variables take precedence over `.env`; GitHub also accepts `GH_TOKEN` as a fallback. `doctor` checks source access but does not validate or post to Slack.

Never commit `.env`, credentials, private reports, or local databases. The CLI suppresses verbose HTTP logging that could otherwise expose query-string credentials, and cached URLs redact sensitive parameters.

## Optional YouTube appendix

When explicitly enabled, the appendix generates up to two event-specific searches for selected parent events within the YouTube candidate limit. Search requests use `type=video`, relevance ordering, an English relevance-language preference, and a configurable publication window. It does not score video suitability or validate each extracted development separately.

YouTube search can still return noisy or loosely related videos. V1 preserves YouTube-returned content and order for manual inspection and deliberately avoids an expanding list of negative keywords. It does not silently filter results or calculate a default relevance ratio, crowding score, views-per-hour metric, creator tier, or YouTube-derived Opportunity Score.

Optional deterministic title/channel annotations exist behind `youtube.enable_local_relevance_annotations`, disabled by default. Enable them only after reviewing and complying with YouTube's applicable derived-metrics terms. See the [YouTube API Services Developer Policies](https://developers.google.com/youtube/terms/developer-policies), [`search.list` documentation](https://developers.google.com/youtube/v3/docs/search/list), and [derived metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy).

**Retention:** live-only YouTube handling and automatic source-specific expiry cleanup are not implemented. API responses can remain in the HTTP cache, database scan records, and generated reports; cache expiry is not deletion. Manage stored data according to source requirements. Reports are evidence snapshots, not a guarantee of exact replay, and source access does not grant unrestricted redistribution rights.

## Evidence and optional LLM assessment

LLM-enabled scans assess captured GitHub notes, official articles, HN-linked pages,
repository READMEs and Hugging Face model/Space cards. Source-aware readers prefer
documentation text to large platform UI pages. Complete captured text is reused;
insufficient, inaccessible or oversized documents remain unassessed.

Every assessed entry answers what changed, who should care and the practical
difference, with literal source quotes, caveats and optional investigation questions.
Publisher statements, community reports and reported tests are labeled. Radar does
not reproduce tests or certify claims. Repeated announcements are not independent
confirmation.

Calls are sequential, bounded and stop after three consecutive new-call failures.
The report exposes source-family coverage, per-source outcomes, page reads, calls,
cache hits, abstentions and skips. Assessment gaps mark the scan partial without
preventing a usable report.

Public-page reads use no cookies, authentication, proxy credentials or browser
execution. DNS answers are validated and connections pinned; redirects are
revalidated. Private/local/reserved targets, credential-bearing URLs, nonstandard
ports, HTTPS downgrades, oversized bodies and unsupported content types are blocked.
Documents are limited to 256,000 response bytes and the configured text cap.

The no-tools model adapter runs in an isolated temporary read-only directory with
project instructions/tools disabled and a sanitized environment. Source credentials,
YouTube data and Slack secrets are not supplied to the model.

Enabling assessment sends source documents to the model service and consumes your
allowance. Confirm permission, especially for private repositories. Caches contain
source text and output and must stay private. Disabling assessment does not delete
existing caches or invoke the model.

Developer caches use `developer-<key>.json` under the database-derived `.llm/`
directory. Identity includes evidence, audience, model/effort, adapter, prompt and
schema. Old video-oriented assessments cannot supply developer scores. Failures
are cached; move only the corresponding cache file aside to explicitly retry after
fixing the cause. Concurrent scans are not coordinated.

## Development and evaluation

```bash
uv run pytest
uv build
```

Tests use mocked providers/models and temporary storage. See
[CONTRIBUTING.md](CONTRIBUTING.md) for regression and wheel checks.

The [extraction experiment](experiments/release_extraction/README.md) remains a
historical extraction-only comparison, not validation of the new impact rubric.
The [agreed plan](docs/developer-first-plan.md) records acceptance criteria.
The [validation record](docs/developer-first-validation.md) documents the isolated
developer-first review and remaining evidence gaps.
The [strategic review](docs/plan_v2.md) includes a v0.2.0 implementation audit;
[the original plan](docs/PLAN.md) is historical.

For live review, use isolated configuration/database/reports and omit Slack.
The helper below snapshots the previous report and starts fresh observation state:

```bash
uv run python experiments/developer_review.py --config config.toml --output reports/developer-review-NEW
```

The output directory must not exist. This consumes configured model allowance;
fresh state cannot reproduce production growth baselines or exact historical ranking.

Replay captured evidence without a second source crawl (zero new model calls by default):

```bash
uv run python experiments/developer_review.py --config config.toml --output reports/developer-replay-NEW --replay-from reports/developer-review-NEW
```

Add `--max-calls N --retry-failed` only to explicitly allow bounded new/retried
assessments. Replayed documents are historical artifacts, not freshly revalidated
HTTP responses; missing documents stay unavailable. Normal scans still enforce cache expiry.

Do not use production state to test migration or overwrite a previous report.
Explicit feedback can document usefulness and misses; self-selected counts are not
precision, recall or proof of productivity gains.

## Current limitations

- Impact scores require human judgment and are not calibrated to measured outcomes.
- Discovery and assessment are bounded; unassessed does not mean unimportant.
- No autonomous verification, test execution, semantic story graph or guaranteed
  cross-source deduplication. Changed source anchors can create related new topics.
- Current documentation may describe an existing capability, not a new release.
- Growth is measured only after local observation starts.
- Source quotas, availability, document size and parsing constrain coverage.
- Optional Slack is the only notification integration. No dashboard, hosted service,
  historical backfill or new social provider is included.

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
