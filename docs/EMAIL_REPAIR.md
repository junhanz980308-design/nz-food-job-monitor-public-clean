# Email extraction repair and controlled recovery

## Evidence and limits

The reported production counts show recognized SEEK alerts with zero extracted jobs. Code review confirmed that opaque links were never resolved, some card/title structures were unsupported, and both mailbox transports acknowledged messages even when extraction returned no jobs. Successful mailbox access was therefore reported as successful monitoring despite lost extraction. Previously acknowledged hashes also caused later scans to skip those emails.

No private mailbox, real email body or tracking URL was accessed during this repair. Synthetic tests prove the supported code paths, not that every current platform template works. A reviewed, manually triggered diagnostic backfill is needed to confirm positive per-platform extraction against the actual mailbox. Zero Indeed/LinkedIn links alone does not establish those emails were job alerts.

## Supported paths and privacy boundaries

- SEEK numeric job URLs, including nested/double-encoded destinations and first-party opaque tracking redirects.
- LinkedIn numeric or slug job views, comm links, and job view/search links carrying a specific currentJobId/jobId.
- Indeed viewjob, rc/clk, pagead/clk, clk and company/clk links with jk; opaque first-party click redirects when the server provides an HTTP Location leading to a supported job URL.
- Nested table title spans/strong text, accessibility labels and image titles, plus merging HTML and plain-text alternatives. Adjacent unresolved cards must not donate titles or context.

Tracking resolution uses HTTPS HEAD only, TLS verification, same-platform official domains, no proxy/cookie/auth inheritance, no automatic redirects, at most four hops and 24 requests per scan with a 45-second start deadline and four-second request timeouts. Local/private DNS addresses are rejected. The resolver stops as soon as it recognizes a job URL: it never requests the destination job page. Existing sender-domain validation is unchanged.

External opaque email vendors, JavaScript/meta-refresh/GET-only redirects, logins, access-control responses and unsupported destination patterns remain unresolved. There is no browser automation, protected-page scraping or access-control bypass. Embedded external wrappers can still be decoded offline without contacting that vendor. If live diagnostics show these unsupported forms, do not broaden the allowlist blindly.

Counters use fixed categories only: direct/embedded job, opaque first-party/external tracker, navigation, unsupported first-party, external link and unsafe URL. Failure reasons include missing title, no extractable jobs, redirect denied/failed/loop/budget, uncertain message and parser exception. No hosts, paths, raw URLs, message IDs, subject lines, addresses or body snippets are emitted.

## Acknowledgement and retry

Only fully extracted alerts and safely classified unrelated messages are acknowledged. Partial cards, failed redirects and uncertain opaque messages remain deferred; unrelated newsletters, career advice and recommended-reading emails can be skipped. Unknown sender domains remain excluded.

Encrypted state adds mail_retry (hashed message IDs only). Retry IDs override old acknowledgements. A partial scan processes valid jobs, reports PARTIAL and returns nonzero instead of a healthy zero-result run. Its cursor is pinned so unresolved messages do not age out merely because the next scan ran. Existing successful job deduplication prevents repeated extracted cards from notifying again. At the 500-message safety limit, split recovery into smaller date windows; persistent unsupported messages remain visible rather than silently discarded.

## Backfill — only after review and explicit approval

Do not reset state, replace MONITOR_STATE_KEY, enable schedules or copy history from the private repository. This change neither runs backfill nor changes schedule enablement. Review/merge and production execution are separate approvals.

1. Once the reviewed workflow is on the default branch, manually select **Silent email alert backfill**. Use a short historical UTC date window (since inclusive, until exclusive; one to seven days, preferably one). The IMAP server applies calendar-day search semantics; overlapping windows are safe.
2. Leave apply=false for the first diagnostic pass. This decrypts existing state and re-reads previously acknowledged emails in the bounded window. It sends no Telegram, calls no Adzuna and does not persist state. Review fixed counters: alerts recognized, extracted jobs, deferred messages, unsupported patterns and failure reasons. Positive extraction with zero unexplained partial failures is the acceptance check; no-emails/marketing-only counts are not evidence of parsing success.
3. After inspecting results, manually rerun the same window with apply=true. Recovered jobs become a silent historical baseline. Existing notification flags and other-source baselines are preserved. Successful message acknowledgements are merged; unresolved IDs are removed from the acknowledged set and placed on retry, with the scan cursor widened only as needed.
4. Repeat the same window safely or advance to the next window. Repeats preserve job deduplication and do not send Telegram. A PARTIAL result exits nonzero but preserves safe recovery progress when apply=true. History continues through the existing encrypted state helper; only ciphertext can enter Git/recovery artifacts. The key and branch format remain unchanged.
5. Resume scheduled monitoring only after separate confirmation. Follow-up counters should show genuine extracted jobs or legitimate zero new mail, not recognized alerts silently acknowledged after failure.

Local equivalent (with credentials already supplied privately and the correct decrypted runtime state):

```sh
python -m scripts.backfill_alerts --since YYYY-MM-DD --until YYYY-MM-DD
# Only after inspecting the diagnostic result:
python -m scripts.backfill_alerts --since YYYY-MM-DD --until YYYY-MM-DD --apply
```

The local command alone does not load or publish encrypted branch state; the manual workflow wraps it with the existing encrypted load/save steps. Never upload plaintext state or paste diagnostic input into public issues.
