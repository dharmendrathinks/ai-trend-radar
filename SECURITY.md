# Security Policy

## Supported version

Security fixes currently target the latest code on the `main` branch and the latest published release, when releases exist.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository when it is available: open the repository's **Security** tab and choose **Report a vulnerability**.

If private vulnerability reporting is not enabled, do not publish credentials, exploit details, or sensitive reproduction data in a public issue. Open a minimal issue asking the maintainer to provide a private reporting channel, without including sensitive technical details.

Include the affected version or commit, impact, reproduction conditions, and any suggested mitigation in the private report. Please allow reasonable time for investigation before public disclosure.

For accidentally exposed third-party API credentials, revoke or rotate the credential with the provider immediately; a source-code fix alone does not invalidate a leaked token.

## Optional model extraction

`scan --llm` or `[llm] enabled = true` sends selected captured GitHub release notes to the model service using the operator's Codex login. Confirm permission before enabling it for private repositories. Release notes are treated as untrusted input; the adapter disables tools and project instructions, uses a temporary read-only working directory, and excludes source/Slack credentials from the child environment. Literal-quote validation does not establish factual correctness of generated interpretations.

Local LLM cache files (by default `data/radar.llm/`) contain source notes, model output, and extraction metadata. Keep these and live reports out of public commits. Disabling extraction does not erase existing cache data. Cached failures are not retried until the relevant cache entry is moved aside or extraction inputs change.
