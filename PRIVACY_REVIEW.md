# Public deployment privacy review

Updated 2026-10-10. All changes are confined to this clean public edition. No original private repository files were changed; no repository was created, published or pushed.

## Deployment design

- Scheduled fast monitoring supports public repositories every ten minutes. Broad Adzuna monitoring remains every four hours to protect the API allowance.
- Telegram, Gmail and Adzuna values are read only from repository Secrets. MONITOR_STATE_KEY is an additional required Secret, supplied only to state load/save steps.
- History is authenticated and encrypted with Fernet before any Git blob is created. Only seen.enc is written to monitor-state. No credentials, email bodies or raw job URLs are intentionally collected in state; encryption additionally protects hashes and source metadata from public correlation.
- Recovery artifacts contain ciphertext only. Cleanup removes plaintext state from the runner. Wrong/missing keys, tampering and legacy plaintext branches stop processing without resetting history. No real key or credential is included here.
- Logs omit source-provided details, free-form source names and exception messages. Summaries contain counts and fixed rule identifiers. Public timing, aggregate counts and ciphertext size remain visible.
- README documents key setup/backup, schedule limitations, encrypted recovery, public metadata exposure and duplicate-delivery risk if persistence fails after sending.

## Validation

The previous clean edition passed 106 tests before changes. Private-only policy tests were replaced with encrypted public-deployment tests; existing application tests are retained. The revised full suite passes 108 tests, covering encrypted local-Git round trips, ciphertext-only Git objects, competing writers, missing/wrong keys, corruption, legacy plaintext rejection, encrypted recovery, log privacy and mocked dry-run safety. No real Gmail, Adzuna or Telegram calls were made.

Workflow YAML, staged whitespace and the complete publication file list were checked using a disposable local repository. The final clean folder has no Git history, credentials or old runtime state. The only runtime state supplied is empty and ignored. Synthetic test addresses and dummy tokens are intentional fixtures; the workflow commit author is the public GitHub Actions bot.

This review covers the supplied snapshot. Inspect the staged diff before publication and future updates. Ignore rules and scanning cannot guarantee that later manually added files are safe. Encryption requires protecting the key and trusting code/dependencies with access to Secrets.
