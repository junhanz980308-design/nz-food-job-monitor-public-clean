# NZ Food Job Monitor — public code edition

Monitor New Zealand food science, R&D, product development, related research, and food industry quality, safety and compliance roles. Telegram receives HIGH MATCH and POSSIBLE MATCH alerts. Ambiguous roles remain visible in read-only diagnostic summaries. Scoring never assumes visa sponsorship.

This distribution contains application code and synthetic tests only. It has no account credentials, Git history, email messages or previous monitoring records. Scheduled monitoring can run in a public repository on standard GitHub-hosted runners. Runtime history is encrypted before it is written to Git; credentials belong in GitHub Secrets.

## Install and test

Python 3.11 is used in CI. From this folder:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m pytest -q
```

Tests block external network access and use synthetic jobs and email messages. Tests of state persistence use temporary local Git repositories. They do not contact Telegram, Gmail or live job services.

## Food quality and safety matching

Food QA/QC, food safety, HACCP, quality officer/technologist/manager, auditor/inspector and regulatory compliance roles can qualify for POSSIBLE MATCH when the title or duties establish the relevant role and food-industry context. Newly recognized quality roles receive 24 role points and are capped at 69; existing scientific title rules retain their scores and HIGH MATCH priority. Generic quality titles without food evidence remain AMBIGUOUS. Software/IT testing, construction and unrelated pharmaceutical quality are excluded; staff food benefits alone are insufficient. Citizenship and permanent-residency restrictions still override relevance.

The shared scoring rules apply to future email, employer-board and Adzuna results. Existing Adzuna queries and request frequency are unchanged, so this extension adds no API calls and cannot find roles absent from the configured searches. Historical postings are not resent merely because their scores improve; silent baselines and pending-delivery retries retain their existing behavior.

Email parsing recognizes complete quality and compliance job titles while retaining structured card boundaries and title/employer associations. Quality words near an opaque tracker do not establish a job destination. A verified canonical job identity is still required, and unresolved or ambiguous content remains deferred. Synthetic tests cover known jobs alongside ambiguous trackers; they do not establish the identities or safety of acknowledging any outstanding live SEEK messages.

## Integrations and configuration

`config/sources.json` includes eleven Adzuna NZ queries and the public SGS SmartRecruiters board identifier. The ATS field named `token` means a public board/account identifier, not an authentication secret. Adapters for Greenhouse, Lever, Ashby, SmartRecruiters, Workable and official RSS/Atom feeds remain available. Add only verified official boards or feeds; the other adapters are supported code, not configured coverage.

Official Gmail job-alert parsing supports evidenced SEEK, LinkedIn, Indeed and Trade Me link patterns. Embedded redirect destinations are decoded locally; the application does not scrape these job boards. Real email formats can change, and synthetic tests do not prove every current template works. Per-platform counters distinguish no emails, parsed alerts and unknown coverage.

Supply secrets through the process environment locally or GitHub Actions repository Secrets. Never put values in source files, issues, screenshots or public logs. The application does not automatically load `.env` files.

| Variable | Purpose |
| --- | --- |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Telegram delivery |
| `GMAIL_IMAP_USER`, `GMAIL_IMAP_APP_PASSWORD` | Gmail IMAP, read-only mailbox access |
| `GMAIL_OAUTH_TOKEN_JSON` | Optional Gmail OAuth alternative; takes priority over IMAP |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | Adzuna NZ API |
| `MONITOR_STATE_KEY` | Required encryption key for Actions history |

For optional OAuth setup, `python -m scripts.gmail_authorize` reads a locally supplied `credentials.json` and saves `.secrets/gmail_token.json`. Both are ignored by Git. Never upload either file. No credentials are included with this edition.

## Local operation

```sh
python -m jobwatch.runner --mode all --dry-run
python -m jobwatch.runner --mode fast
python -m jobwatch.runner --mode broad
```

A real dry run still reads configured sources, but sends no Telegram messages and does not save state. Use the automated tests for a fully mocked, offline dry run. Normal runs use ignored `state/seen.json`; a missing file starts empty. `state/empty.example.json` is the only state file intended for publication. Initial results form a silent baseline; subsequent new relevant results can notify. Starting from fresh state loses old deduplication knowledge, so this public copy is not a replacement for existing production history.

The audit reports fetched results, match categories, ambiguous cases, duplicates, baseline suppression and notification history using counts and rule identifiers. It does not print email bodies, private URLs or job identities. Existing history can be audited with `--dry-run` without rewriting it.

## GitHub Actions in a public repository

After publication, add credentials in repository **Settings → Secrets and variables → Actions → Secrets** (not Variables). Gmail IMAP and OAuth are alternatives. No values are included in this folder or placed in workflow files. The automatic `GITHUB_TOKEN` handles the encrypted-state branch with `contents: write`; no personal access token is needed.

Generate a **new** state key locally after installing requirements:

```sh
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Copy that value directly into the `MONITOR_STATE_KEY` repository Secret and keep a secure backup. Do not paste it into chat, documentation, logs or a commit. No key has been generated or added during preparation. Losing or replacing it prevents reading previous history; runs stop rather than silently reset. Changing the key requires a deliberate decrypt/re-encrypt migration with the old key; this edition does not automate rotation.

The workflow supports public and private repositories. Fast monitoring runs at minutes 7, 17, 27, 37, 47 and 57 of each hour (UTC). Adzuna runs at minute 13 every four hours to preserve its quota. Test the initial setup with a manual `all` / `dry_run=true` run, then run normally to establish the silent baseline. A dry run reads sources and decrypts history but neither sends Telegram nor persists changes. The separate Telegram test is manual and intentionally sends one message when explicitly triggered.

Only `seen.enc`, authenticated ciphertext, is committed to `monitor-state`. Encryption happens **before** creating any Git blob. No plaintext state is committed, cached or uploaded as an artifact. On a failed push, only encrypted recovery data can be uploaded for seven days. Plaintext state is removed from the runner during cleanup. Wrong keys, corrupt ciphertext, unknown state fields and legacy plaintext branches fail closed. Never import a private repository's old state branch into the new public repository.

Unsalted hashes alone can reveal which public jobs were observed, so they are not treated as anonymous. Encryption also protects timestamps, mailbox deduplication hashes and configured source labels. Public observers can still see commit/run times, ciphertext sizes and aggregate diagnostic counts; no email content, personal addresses, job URLs or credentials are intentionally included in public logs or readable history. Keep the encryption key secret: its disclosure could expose all historical ciphertext snapshots. Recovery artifacts are not a substitute for a key backup. Source details and exception text are omitted from logs; diagnostics use counts and fixed rule identifiers.

Serialized workflows and ordinary fast-forward pushes prevent overwriting competing history. Delivery and persistence are not one atomic transaction: a runner crash or failed push after Telegram delivery can allow a duplicate on retry. Preserve encrypted recovery data after a persistence failure rather than deleting the branch. State grows over time; monitor repository storage use.

Eleven queries × six broad runs/day × 31 days = 2,046 Adzuna requests before retries and manual runs. Against a 2,500-request allowance that leaves 454 requests; verify the allowance for your own account. The first 50 results per query and seven-day age filter limit coverage. Scheduled runs can be delayed or dropped; GitHub also disables public scheduled workflows after 60 days without repository activity. Check the Actions page periodically. A ten-minute cron is not a delivery-time guarantee.

Standard GitHub-hosted runners are free for public repositories; other runner types and storage have separate limits. See [GitHub billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [schedule behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), and [Fernet authenticated encryption](https://cryptography.io/en/latest/fernet/).

## Publishing this code

See [PUBLIC_UPLOAD.md](PUBLIC_UPLOAD.md) for an upload checklist. No repository has been created or published as part of preparing this folder. Keep existing private repositories, their secrets and their `monitor-state` branches unchanged.

## Email parsing failures and historical recovery

See [Email repair and controlled backfill](docs/EMAIL_REPAIR.md). Failed/partial alerts now remain retryable and fail visibly. A manual-only, silent backfill can recover previously acknowledged messages without clearing history or replaying old Telegram notifications. Production execution requires separate approval.
