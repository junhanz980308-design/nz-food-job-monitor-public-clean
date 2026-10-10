"""Explicit, bounded, silent reprocessing of previously acknowledged alerts."""
from __future__ import annotations

import argparse
import copy
import json
from datetime import datetime, timezone

from jobwatch.email_alerts import gmail_alerts
from jobwatch.state import State


def backfill(path: str, since: str, until: str, dry_run: bool = True) -> int:
    start = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if not 0 < (end - start).days <= 7 or end > datetime.now(timezone.utc):
        raise ValueError("Use a past interval of one to seven whole UTC days")
    state = State(path)
    scan = copy.deepcopy(state.data)
    # Ignore old message acknowledgements only for this bounded scan.
    scan.update(mail_seen=[], mail_retry=[], mail_scan_after=start.isoformat(), mail_scan_before=end.isoformat())
    result, acknowledged = gmail_alerts(scan)
    if result.status not in {"ALERT_ONLY", "PARTIAL"}:
        raise RuntimeError("Mailbox is not configured")
    recovered = 0
    for job in result.jobs:
        if not job.title.strip() or not job.url.startswith("https://"):
            continue
        ident, new = state.observe(job)
        if new:
            # Baseline recovered jobs silently; never replay historical postings.
            state.mark_notified(ident)
            recovered += 1
    retry = set(state.data.get("mail_retry", [])) - set(acknowledged)
    retry.update(scan.get("mail_retry", []))
    state.data["mail_retry"] = sorted(retry)
    state.data["mail_seen"] = sorted((set(state.data.get("mail_seen", [])) | set(acknowledged)) - retry)
    if retry:
        state.data["mail_scan_after"] = min(state.data.get("mail_scan_after", start.isoformat()), start.isoformat())
    # Do not reset initialized_sources, delivery flags, API baselines or the key.
    if not dry_run:
        state.save()
    print("BACKFILL_SUMMARY: " + json.dumps(dict(dry_run=dry_run, recovered_baseline=recovered,
          extracted=len(result.jobs), incomplete=result.status == "PARTIAL", email_platforms=result.counters), sort_keys=True))
    return 1 if result.status == "PARTIAL" else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", default="state/seen.json")
    parser.add_argument("--since", required=True, help="YYYY-MM-DD inclusive UTC day")
    parser.add_argument("--until", required=True, help="YYYY-MM-DD exclusive UTC day")
    parser.add_argument("--apply", action="store_true", help="Persist silent baseline and corrected acknowledgements")
    args = parser.parse_args()
    try:
        result = backfill(args.state, args.since, args.until, not args.apply)
    except Exception:
        raise SystemExit("Backfill failed; details omitted for privacy. Check date bounds, credentials and scan limits.") from None
    raise SystemExit(result)


if __name__ == "__main__":
    main()
