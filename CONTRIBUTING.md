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
- Keep deterministic Discovery Priority distinct from editorial Developer Priority. The unified report, topic review ledger and Slack brief must use the same developments. Mock all model calls; test literal source-block quotes, invalid caches, disabled mode and failure isolation.
- Test /100 bounds, the 50/30/20 calculation, floors, independent sibling ranking, N/A-last behavior, unknown dates and audience cache invalidation. Urgency is not freshness. Version/document rubric changes; the historical extraction-only experiment does not evaluate developer ranking.
- Preserve fair bounded assessment across all existing source families. Test document/model caps, public-address validation, DNS pinning, redirects, size bounds, source-aware README/model-card URLs and source-time attribution. Never replace missing page evidence with headline-only impact scores.
- Test stable source-anchor IDs under generated rewording, conservative ambiguous matching, independent topic decisions, legacy migration/backups, inherited review state without invented feedback, overflow and unchanged-scan suppression.
- Update public documentation when configuration or output changes.

Scoring weights and eligibility thresholds are product behavior. Do not casually tune them to improve one scan; propose such changes with evidence, before/after examples, and regression tests.

Keep commits small enough to review, but do not rewrite authentic development history solely for cosmetic reasons.

The Codex adapter and packaged schemas/prompts live in `src/ai_trend_radar/llm_adapter.py` and `src/ai_trend_radar/llm_assets/`. The extraction experiment shares the adapter but retains its old extraction-only contract. Check both old and developer schema validation from an installed wheel. A live `scan --llm` sends bounded source documents to the model service and consumes allowance; it is not part of CI. Use isolated state and never add `--slack` unless delivery is explicitly intended. See [the active plan](docs/developer-first-plan.md) and [design history](docs/README.md).
