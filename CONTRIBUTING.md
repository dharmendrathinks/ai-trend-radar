# Contributing

Thanks for helping improve AI Trend Radar. Keep contributions focused, explainable, and easy to verify.

## Setup

```bash
git clone https://github.com/dharmendrathinks/ai-trend-radar.git
cd ai-trend-radar
cp config.example.toml config.toml
cp .env.example .env
uv sync --extra dev
```

No credentials are needed for the automated test suite.

## Before opening a pull request

```bash
uv run pytest
uv build
```

For changes involving live providers, also run `uv run ai-trend-radar doctor` and an appropriate opt-in smoke test. Never include credentials, `.env`, `config.toml`, local databases, caches, or generated live reports in a commit or issue.

Pull requests should:

- Describe the problem and the observable behavior change.
- Add or update tests for normalization, eligibility, scoring, query generation, or reporting behavior affected by the change.
- Preserve provider failure isolation and credential-free tests.
- Keep calculations deterministic and evidence-backed.
- Keep LLM suggestions separate from deterministic scoring, the review ledger, and Slack delivery. Mock model calls in tests; verify literal quotes, cache invalidation, disabled mode, and failure isolation.
- Editorial topic priority is separate from Discovery Priority. Test /100 score bounds, the 30/30/20/20 calculation, global topic ordering, missing dates/scores, audience cache invalidation, and freshness recalculation on cached results. Model judgments are not measured audience demand. Version and document rubric changes; the extraction-only experiment does not evaluate editorial ranking.
- For HN page enrichment, preserve the shared model-call cap and main-list/page limits. Test public-address validation, DNS pinning, redirects, response/text bounds, page-cache reuse, failure isolation, and the distinction between HN submission time and product launch time. Never replace linked-page evidence with a headline-only score.
- Update public documentation when configuration or output changes.

Scoring weights and eligibility thresholds are product behavior. Do not casually tune them to improve one scan; propose such changes with evidence, before/after examples, and regression tests.

Keep commits small enough to review, but do not rewrite authentic development history solely for cosmetic reasons.

The production Codex adapter and its prompt/schema live in `src/ai_trend_radar/llm_adapter.py` and `src/ai_trend_radar/llm_assets/`. The extraction experiment imports this same adapter. Changes to these files invalidate normal-scan caches; run the experiment regression tests too. Confirm the wheel includes both assets. A live `scan --llm` is opt-in, sends configured GitHub notes to the model service, and consumes model allowance; it is not part of CI. Do not add `--slack` to validation runs unless delivery is explicitly intended.
