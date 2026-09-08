# Developer-first validation — September 7, 2026

This records the September 7 validation of `0.3.0.dev0`. The
[agreed plan](developer-first-plan.md) is implemented; editorial usefulness still
needs human review. No commit, push, release, schedule change or Slack delivery
was performed during that validation. On September 8, the user authorized the
v0.3.0 release; see [release notes](../RELEASE_NOTES.md) for its scope and upgrade guidance.

## Automated checks

- Pre-change baseline: 190 passing tests. Updated suite: 217 passing tests on
  Python 3.12 and Python 3.13.
- Coverage includes source-family budgets, no-network captured replay, literal
  quote rejection, score arithmetic/floors, N/A ordering, consequential fixes,
  stable identities across model paraphrases, review isolation, migration backup
  and rollback, inherited legacy decisions without fabricated feedback, and
  report/brief/Slack consistency.
- Package build and installed-wheel checks verify the CLI plus legacy and new
  developer assessment assets. The lockfile is consistent.
- PR Ready's deterministic analyzer is run separately. It does not run this
  Python project's tests/build because there is no `package.json`; the checks
  above supply that evidence rather than treating skipped checks as passes.

## Isolated source and LLM review

Local artifacts are ignored generated files, not committed report fixtures:

- Initial live scan: `reports/developer-review-20260907/latest.md`, scan
  `be6cc68669`. All five collection providers returned successfully; 19 new model calls.
- Final captured-evidence replay:
  `reports/developer-review-20260907-final/latest.md`, scan `40cbfc357c`.
  Five additional model calls, seven cached assessments, no provider or document
  refetch. Total validation allowance consumed: 24 new model calls.
- Final assessment considers 40 of 96 candidate events: 11 assessed events,
  one assessment failure, one abstention, 85 skipped events (some considered
  events cannot proceed within the other budgets). The top-limited report shows
  10 scored developments and six Watch entries; 102 additional qualifying scored
  or unassessed developments remain available to the changes inbox.
- Both scans are explicitly **partial**, not exhaustive AI-news rankings.
  Replay uses captured source evidence and records that it was not re-fetched.

The live pass exposed an overly large HTML evidence block that grouped unrelated
changes under one anchor. Long blocks now split deterministically into smaller
source spans, with a regression test; the final replay successfully assessed the
previously rejected MCP architecture article.

Pod remains unassessed: the model altered a Markdown-containing quote, and literal
source validation rejected the response again on retry. This is a visible recall
limitation, not a low usefulness judgment. Validation was not loosened to admit it.
No further retry was made beyond the total allowance.

## Qualitative comparison and limits

The saved schema-2.3 baseline led with whole projects such as Pod and Engrim.
The new report instead exposes individual workflow consequences: deletion-command
safeguards, resumed-session context, content-exclusion enforcement, and explicit
agent execution results. Separate developments from a shared release rank and
remain independently reviewable. The three-question cards and score reasons make
the affected developers and practical consequences explicit.

That is a presentation/coverage observation, not proof of better precision or
productivity. The baseline and live pass differ in collection time and observation
state; fresh isolated databases do not reproduce production growth ranking.
Literal quotes support attribution, not the truth of publisher claims. Sources
and recommended products were not independently tested. Current architecture
guidance and READMEs may describe existing capabilities rather than recent launches;
the report preserves date provenance and caveats. Cross-source duplicates and
meaningful source-anchor changes still require human judgment.

## Production safety and documentation

Production `data/radar.sqlite3`, `reports/latest.md`, `reports/latest.json` and
`config.toml` retained their original modification times and sizes throughout
validation. Migration tests used temporary databases; the live/replay helper used
new, separate databases and report directories. No production migration was run.

README, configuration example, release notes, security/contribution guidance,
package metadata, experiment instructions and CI packaging checks were updated.
`PLAN.md` and `plan_v2.md` moved into `docs/`; links were updated. The strategic
review now explicitly audits the shipped v0.2.0 LLM work and distinguishes its
historical recommendations from this developer-first redesign.
