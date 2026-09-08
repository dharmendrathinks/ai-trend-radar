# Security Policy

## Supported version

Security fixes currently target the latest code on the `main` branch and the latest published release, when releases exist.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository when it is available: open the repository's **Security** tab and choose **Report a vulnerability**.

If private vulnerability reporting is not enabled, do not publish credentials, exploit details, or sensitive reproduction data in a public issue. Open a minimal issue asking the maintainer to provide a private reporting channel, without including sensitive technical details.

Include the affected version or commit, impact, reproduction conditions, and any suggested mitigation in the private report. Please allow reasonable time for investigation before public disclosure.

For accidentally exposed third-party API credentials, revoke or rotate the credential with the provider immediately; a source-code fix alone does not invalidate a leaked token.

## Optional model extraction

`scan --llm` or `[llm] enabled = true` sends selected captured documents from all existing source families (release notes, official articles, HN-linked pages, public repository READMEs and model/Space cards) to the model service using the operator's Codex login. Confirm permission before enabling it for private repositories. All source content is untrusted; the adapter disables tools and project instructions, uses a temporary read-only working directory, and excludes source/Slack credentials from the child environment. Literal-quote validation and source-block identity do not establish factual correctness or independent verification.

Local LLM cache files (by default `data/radar.llm/`) contain source notes, model output, and extraction metadata. Keep these and live reports out of public commits. Disabling extraction does not erase existing cache data. Cached failures are not retried until the relevant cache entry is moved aside or extraction inputs change.

Additional document reads are capped at 12 per scan by default, across all source families, including at most five HN candidates. HN assessment no longer requires main-list admission. Set `llm.max_hn_stories = 0` to disable that lane, or `llm.max_page_fetches = 0` to prohibit additional document reads. The reader sends no authentication, cookies, source tokens, or proxy credentials; it validates public DNS answers and pins the socket to the validated IP while preserving TLS hostname verification. Each redirect is checked again. Private/local/reserved addresses, nonstandard ports, credential-bearing URLs, HTTPS downgrades, oversized bodies, and unsupported content types are blocked. It does not execute scripts, register accounts, or submit observations. Treat page text as untrusted input. Its local cache (`data/radar.pages/`) is ignored and should remain private alongside model output and reports.

Schema-3 migration preserves legacy history and writes a pre-migration SQLite backup
beside the database. That backup contains the same sensitive data as the original.
Default `data/` and `reports/` locations are ignored; custom locations require their
own exclusions. Do not publish backups, source documents or unrestricted model output.
