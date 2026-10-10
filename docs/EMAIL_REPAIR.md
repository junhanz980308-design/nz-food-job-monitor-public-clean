# Email extraction repair and controlled recovery

## Evidence and limits

The reported production counts show recognized SEEK alerts with zero extracted jobs. Code review confirmed that opaque links were never resolved, some card/title structures were unsupported, and both mailbox transports acknowledged messages even when extraction returned no jobs. Successful mailbox access was therefore reported as successful monitoring despite lost extraction. Previously acknowledged hashes also caused later scans to skip those emails.

The initial repair used synthetic tests without accessing a private mailbox or original email samples. Synthetic tests prove the supported code paths, not that every current platform template works. A reviewed, manually triggered diagnostic backfill is needed to confirm positive per-platform extraction against the actual mailbox. Zero Indeed/LinkedIn links alone does not establish those emails were job alerts.

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

## Follow-up: card-aware redirect selection

A successful job extraction and a failed tracking link are not necessarily two different jobs. Earlier selection attempted nearly every tracker, including redundant card buttons and MIME duplicates; its failure counters counted link occurrences rather than necessarily distinct jobs. The old `redirect_unresolved` label meant no resolver was used (insufficient email-level evidence), not a failed network request and not proof that the link represented a job.

The parser now inspects all links in each message before issuing requests. Explicit navigation/marketing controls are separated from job-title links, job CTAs, contextual card links and ambiguous tracking targets. Semantic cards, title boundaries, multiple job IDs, nested cards and linked sibling rows/blocks prevent one job from satisfying a neighbor. When a card has one canonical job identity, its duplicate title/button destinations can reuse it; after a successful redirect the other links in that same card can reuse that result. Exact normalized targets across HTML/plain-text alternatives share work, as do shared intermediate redirect hops. Only fragments are removed from request identities; signed query parameters are preserved.

Resolution order within each message is job titles, job CTAs, contextual card links, then unsupported job-shaped links. Ambiguous trackers receive no speculative request and remain unresolved. A marketing-looking subject does not authorize acknowledging an email with unexplained opaque links. MIME duplicates of an explicitly identified navigation link inherit its navigation classification only when there is no conflicting job evidence. The scanner still reads messages in transport order; this is not global prioritization across the entire mailbox.

No budget or network allowlist is expanded: 24 HEAD requests per scan, four hops, a 45-second start deadline and four-second request timeout remain. No GET fallback, job-page fetch, external tracker request, login or access-control bypass is added. HTTPS/userinfo/port/domain/DNS checks are retained. Navigation path recognition uses explicit path boundaries so opaque signature substrings are not mistaken for navigation.

### Reading the new counters

| Counter/category | Meaning |
| --- | --- |
| `link_occurrences`, `unique_link_targets`, `duplicate_links` | Raw HTML/text occurrences versus normalized distinct targets; none of these is a job count |
| `link_decisions` | Fixed labels: canonical job, job title, job CTA, card link, navigation, marketing, ambiguous tracking, unsupported job content, cross-platform job or other |
| `canonical_card_reuse` | Redundant card/MIME links covered by one known job without a request |
| `redirect_requests`, `redirect_cache_hits` | Actual HEAD requests versus reused resolver results |
| `unresolved_job_targets`, `ambiguous_targets` | Distinct selected job-evidence targets versus opaque targets without enough evidence; these are still not a count of missing jobs |
| `insufficient_job_evidence`, `resolution_unavailable` | No speculative request or no resolver available; distinct from network rejection |
| `reject_non_https`, `reject_userinfo`, `reject_port`, `reject_malformed_url` | URL syntax/transport restriction |
| `reject_external_domain`, `reject_cross_platform` | Destination outside the allowed sender platform |
| `reject_navigation_path`, `reject_unsupported_path` | Known navigation endpoint or path outside supported tracking/job patterns |
| `reject_nonpublic_dns`, `redirect_dns_failed` | Non-public/missing DNS result or failed lookup |
| `redirect_access_denied`, `redirect_rate_limited`, `redirect_head_unsupported` | HTTP 401/403, 429 or 405; no bypass/fallback is attempted |
| `redirect_missing_location`, `redirect_not_http`, `redirect_server_error` | Redirect without Location, non-redirect response or server failure |
| `redirect_request_budget`, `redirect_time_budget`, `redirect_hop_limit`, `redirect_loop` | Separate resource/loop limits |
| `ambiguous_card_identity` | The same opaque target appears in separate cards with conflicting canonical identities; do not borrow one identity |
| `redirect_request_failed`, `missing_title` | Request failure without exception text, or a supported job with no safe title |

Reasons for unresolved targets are counted once per normalized target per message, while classification decisions count occurrences. Repeated messages may still count the same target separately, with a resolver cache hit and no new request. Reasons never contain dynamic hosts, URLs, HTTP response bodies, subjects or addresses.

Synthetic regression tests contrast forty explicit marketing/navigation links (zero jobs, zero redirect requests) with forty opaque unexplained targets (still deferred, no assertion that they are jobs). They also cover canonical reuse, mixed MIME, prioritization, neighbor isolation, shared redirect hops and failure categories. These demonstrate the policy; they do not identify the actual targets in any private email. Confirm the remaining real-template issues with a separately approved, read-only diagnostic backfill. Do not use apply=true or resume scheduled monitoring as part of this review.

This follow-up does not modify acknowledgements/retry storage logic, encryption, MONITOR_STATE_KEY, state history, Telegram, Adzuna or workflow schedules.


## SEEK linked tables and MIME pairing

SEEK also sends deeply nested tables inside a single block link. The title is
an underlined division in a bold table cell; the next row explicitly names the
employer. These are separate from location and salary rows. Treat the link as
the card boundary, including when the title has no scientific-role keywords.
Do not concatenate its metadata into the title or borrow from adjacent cards.

Plain-text alternatives can carry bracketed canonical job URLs while HTML uses
opaque trackers. Tokenize bracketed URLs and inline image/link pairs separately.
Pair a text URL to an HTML card only when its own preceding text block contains
the exact consecutive title and employer and that combination identifies one
HTML card. Multiple identities for one card remain a conflict; duplicate
combinations are not matched by order. Known MIME identities avoid network
requests. Different unresolved MIME trackers for the same established card do
not spend the request budget twice after a failure.

SEEK home, app-store, alert-management, subscription and clearly labeled advice
controls are classified independently of job cards. Bracketed image references
are recognized using image syntax or corroborating HTML image labels. A generic
unexplained footer tracker still causes deferral. No allowlist, sender validation,
HTTPS, DNS, access-control or request limit is relaxed.

Additional fixed counters:

- `structured_job_cards`: recognized linked-table card occurrences, not a count
  of every supported platform/template and not necessarily unique jobs.
- `unresolved_job_cards`: those cards without an identifiable job, safe title,
  or unambiguous MIME identity. The message remains deferred.
- `mime_card_reuse`: replacements using a canonical identity from an exactly
  matched text card. These are also included in `canonical_card_reuse`.
- `job_card` and `decorative_image`: structural job evidence and image-only link
  decisions, respectively.
- `card_resolution_deferred`: another MIME link for an already failed card;
  no additional request. Consult that card's original fixed failure category.

A title failure is counted per message by `missing_title`, not per missing job.
Aggregate historical extraction totals cannot identify which job was lost, or
prove whether a changed count removed a false positive. Opaque-only identities
cannot be verified by offline parsing; these alerts must stay deferred until a
separately approved safe diagnostic can establish their destinations.

The follow-up was checked locally against privately supplied samples with all
network resolution disabled. Their contents and sample-derived diagnostics are
not included in this repository. The committed fixtures are hand-written,
synthetic structures. Automated tests cover MIME identity matching, nested title
and employer extraction, navigation/image classification, conflicts, retries,
old notification deduplication and a fully mocked dry-run that preserves state.
No production history, acknowledgement, key, integration configuration or
schedule is changed or executed by this repair.
