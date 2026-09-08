# Developer-first redesign — agreed implementation plan

Agreed September 7, 2026. Implemented in `0.3.0`; release authorized September 8.
See [validation results](developer-first-validation.md) and
[release/upgrade notes](../RELEASE_NOTES.md). Editorial scores still require human review.
This document supersedes conflicting creator-first recommendations in `plan_v2.md`.

## Purpose and decisions

> What changed, which developers should care, and what difference could it make to their work?

Serve developers using AI in their work and building AI-powered software. Cover
releases, consequential fixes, breaking changes, pricing/access changes and
evidence-backed practical findings. Reliability, local AI and affordability are
examples, not hardcoded priorities. No video-worthiness scoring.

Use one numbered list across official feeds, GitHub, Hacker News and Hugging Face.
Rank and review individual developments, including multiple changes in one release.
Keep the CLI, Markdown/JSON reports, changes brief, feedback and optional Slack.
No dashboard, new discovery provider, autonomous corroboration or product testing.

## Discovery and evidence

- Assess before top-results cutoffs, including HN outside the old main list.
- Replace video-angle gating with developer-consequence extraction. Consequential
  fixes remain eligible under maintenance headings. Engagement and short freshness
  floors do not exclude assessment; preserve relevance and configured lookback.
- Read complete captured release/announcement text or bounded public article,
  README and model/Space-card text. Metadata alone cannot justify an impact score.
- Default bounds: 40 candidate events, 12 additional documents, 24 new model calls,
  existing timeout/input caps and GitHub/HN lane limits. Round-robin source families
  for assessment opportunities; discovery order within each family. At most three
  developments per event, one call per event, with fitting captured support.
- Report counts and exact reasons for failures, abstentions, caps and missing text.
- Every assessed development explains what changed, affected workflows,
  consequences, prerequisites, evidence and caveats, optionally what to inspect next.
- Distinguish publisher statements, community reports and reported tests. Literal
  quote validation is not independent verification. Preserve model tool isolation,
  credential isolation and public-target/redirect/content-size protections.
- Cache identity covers evidence, audience, model, adapter, prompt and schema.

## Developer Priority v1 and presentation

| Category, each /100 with a reason | Weight |
|---|---:|
| Developer impact: practical consequence for affected developers | 50% |
| Developer relevance: fit to AI users and builders | 30% |
| Urgency: how promptly affected developers should investigate/respond | 20% |

Integer components, weighted overall calculated in code to one decimal. Document
0/25/50/75/100 anchors. Urgency is circumstance-based, not age-based or a live
deadline countdown. Scores are editorial judgments as of the assessment time.
Age, evidence type and uncertainty remain separate. Unknown dates do not invalidate
supported impact assessments; HN submission and source modification are not launches.

Main-list floors: overall 50, impact 25, relevance 50. Lower scores enter Watch.
Sort assessed topics by overall, impact, relevance, urgency, valid event time and
stable ID. Follow with qualifying unassessed discoveries marked N/A, sorted by
deterministic Discovery Priority. Explain that N/A is not low usefulness. With LLM
disabled, provide source-grounded discovery ordering, never invented impact scores.
`--top` limits the combined main list; fewer results are valid. Replace the generic
source entry when individual developments exist. Put diagnostics and Watch below.
Keep YouTube opt-in and off by default, in an appendix only.

## Identity, review and compatibility

- Persist development IDs from source anchors, not generated titles/scores/order.
  Require references to stable source-block IDs. Reuse unchanged anchors; ambiguous
  matches remain separate and related, never silently merged review state.
- Material evidence changes can revise topics; model phrasing or scores alone do
  not create discoveries. Link sibling topics to their shared event/source.
- Align full reports, brief and Slack with topic-level review/defer/reopen/feedback.
  Preserve overflow, stale-revision and retry safeguards. Feedback never learns
  weights or changes review state automatically.
- Replace unassessed placeholders when real developments are found, retaining
  history without copying judgments onto previously unknown changes.
- Report/brief schema 3.0, additive SQLite schema 3 migration with a SQLite backup.
  Preserve observations, legacy events, decisions, feedback and delivery history.
- Inherit current legacy reviewed/deferred state for the initial extracted topic
  set, explicitly labeled inherited. Never invent per-topic usefulness ratings.
  Keep legacy feedback counts separate. Later new developments are independently
  reviewable. Preserve historical report files.
- New commands target topic IDs; explicit `--event` supports legacy operations.
- Add `[developer] audience`; legacy `llm.audience` is a deprecated fallback, with
  the new setting taking precedence. LLM stays opt-in; preserve CLI overrides.
- Add `--youtube`; preserve `--no-youtube` and existing explicit configuration opt-ins.

## Implementation, tests and delivery

Order: contracts/identity → evidence/assessment → scoring/unified reports → review
migration/brief → documentation and end-to-end validation.

Test consequential fixes versus chores; all source families and budgets; independent
sibling ranking; no duplicate generic cards; attributed claims and quote rejection;
arithmetic/floors/ties/unknown dates/N/A; identity under paraphrasing and reordered
sources; conservative ambiguous matching; migration/history/inheritance; sibling
review isolation; unchanged-scan suppression; missing model/unsafe/oversized inputs;
and optional appendix-only YouTube. Run full tests and installed-wheel asset checks.
Pre-change baseline: 190 passing tests and clean worktree.

Update README, configuration, CLI help, package metadata, security/contribution
guidance, experiments, unreleased notes and roadmap status. Validate fixed fixtures,
then one bounded live LLM scan in isolated reports/database. Compare with the saved
report for usefulness, misses, unsupported claims and duplicates.

The original implementation scope excluded pushing or publishing. On September 8,
the user authorized committing, pushing and publishing these changes as v0.3.0.
Do not send test Slack messages, change schedules or replace production scan state
as part of that release. Record verification results separately from this agreed plan.
