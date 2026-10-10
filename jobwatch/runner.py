"""Entry point: fast=Gmail/ATS, broad=Adzuna, all=both."""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .email_alerts import gmail_alerts
from .matching import ALERT_TIERS, match_job
from .audit import Audit
from .models import Job, SourceResult
from .notification import job_message, send_message
from .sources import SourceError, adzuna_search, poll_ats, official_feed
from .state import State

LOG = logging.getLogger("jobwatch")


def process_results(
    state: State, result: SourceResult, candidates: dict[str, tuple[Job, object]],
    *, dry_run: bool, audit: Audit | None = None,
) -> None:
    if result.status not in ("LIVE", "ALERT_ONLY"):
        return
    previously_initialized = state.initialized(result.source_id)
    audit = audit if audit is not None else Audit(state)
    LOG.info("Source accepted; baseline=%s", not previously_initialized)
    for job in result.jobs:
        audit.counts["fetched"] += 1
        if not (job.title or "").strip() or not (job.url or "").startswith("https://"):
            audit.counts["invalid"] += 1
            continue
        match = match_job(job)
        canonical_id, first_seen = state.observe(job)
        record = state.data["records"][canonical_id]
        baseline = not previously_initialized
        if baseline:
            if first_seen:
                state.mark_notified(canonical_id)
            decision = "baseline_suppressed"
        elif state.was_notified(canonical_id):
            decision = "already_notified"
        elif match.tier not in ALERT_TIERS:
            decision = "diagnostic_only" if match.tier == "AMBIGUOUS" else "low_match"
        elif not first_seen and not record.get("alert_pending") and any(state.resolve(key) == canonical_id for key in audit.initial_ids):
            # Legacy notified=False means either rejected or failed delivery.
            # Without stored evidence it is unsafe to replay it after a rule change.
            decision = "historical_suppressed"
        else:
            decision = "retry_pending" if any(state.resolve(key) == canonical_id for key in audit.initial_pending) else "eligible"
            record["alert_pending"] = True
            old = candidates.get(canonical_id)
            preferred = job.source in {"Greenhouse", "Lever", "Ashby", "SmartRecruiters", "Workable"}
            if not old or preferred or match.score > old[1].score:
                candidates[canonical_id] = (job, match)
        audit.record(state, canonical_id, match, decision, baseline)
    state.set_initialized(result.source_id)
    state.health_ok(result.source_id)


def run(config_path: str, state_path: str, mode: str, dry_run: bool = False) -> int:
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    state = State(state_path)
    state.data.setdefault("mail_seen", [])
    audit = Audit(state)
    candidates: dict[str, tuple[Job, object]] = {}
    errors: list[str] = []
    active = 0
    summaries: list[str] = []
    pending_mail_hashes: list[str] = []
    mail_success = False
    scan_start = datetime.now(timezone.utc)

    def accept(result: SourceResult, message_hashes: list[str] | None = None):
        nonlocal active
        if result.counters:
            audit.mail = result.counters
        if result.status in ("LIVE", "ALERT_ONLY"):
            active += 1
            process_results(state, result, candidates, dry_run=dry_run, audit=audit)
            if message_hashes:
                pending_mail_hashes.extend(message_hashes)
        else:
            LOG.warning("Source unavailable or not configured")
        summaries.append("Source accepted" if result.status in ("LIVE", "ALERT_ONLY") else "Source unavailable")

    def error(source_id: str, exception: Exception):
        if hasattr(exception, "counters"):
            audit.mail = exception.counters
        msg = "Source failed"
        errors.append(msg)
        summaries.append(msg)
        LOG.error("Source failed; details omitted for privacy")
        if state.health_failed(source_id):
            try:
                if not dry_run:
                    send_message(f"⚠️ NZ Food Job Monitor: 来源 {source_id} 已连续失败至少3次。请查看 GitHub Actions 运行日志。")
                    state.mark_health_alerted(source_id)
            except RuntimeError:
                LOG.error("Could not deliver source-health warning")

    if mode in ("fast", "all"):
        try:
            mail_state = state.data
            if dry_run:
                mail_state = copy.deepcopy(state.data)
                mail_state["mail_seen"] = []
                # Re-read acknowledged mail for auditing; never change the cursor.
                lookback = (scan_start - timedelta(days=4)).isoformat()
                mail_state["mail_scan_after"] = min(mail_state.get("mail_scan_after", lookback), lookback)
            emails, hashes = gmail_alerts(mail_state)
            accept(emails, hashes)
            mail_success = emails.status == "ALERT_ONLY"
            if mail_success:
                state.data.setdefault("mail_scan_after", (scan_start - timedelta(days=4)).isoformat())
        except Exception as exc:
            error("gmail-alerts", exc)
        for entry in config.get("feeds", []):
            sid = "feed:" + str(entry.get("name", "unknown"))
            try:
                accept(official_feed(entry))
            except Exception as exc:
                error(sid, exc)
        for entry in config.get("ats", []):
            sid = str(entry.get("type", "unknown")) + ":" + str(entry.get("name", "unknown"))
            try:
                accept(poll_ats(entry))
            except Exception as exc:
                error(sid, exc)

    if mode in ("broad", "all"):
        for query in config.get("adzuna_queries", []):
            try:
                accept(adzuna_search(query))
            except Exception as exc:
                error(f"adzuna:{query}", exc)

    sent, failed = 0, 0
    candidates = {state.resolve(key): value for key, value in candidates.items()}
    for canonical_id, (job, match) in sorted(candidates.items(), key=lambda p: -p[1][1].score):
        if not dry_run and state.was_notified(canonical_id):
            continue
        LOG.info("Candidate: [%s score=%d]", match.tier, match.score)
        if dry_run:
            continue
        try:
            # Check secrets before marking jobs as notified.
            send_message(job_message(job, match, state.data["records"][canonical_id]["first_seen"]))
            state.mark_notified(canonical_id)
            sent += 1
        except RuntimeError as exc:
            failed += 1
            LOG.error("Failed to send Telegram job notice: %s", type(exc).__name__)

    if not dry_run:
        # Acknowledge mail only once all alerts were delivered. On failure the
        # next scan reloads those messages; successful jobs remain deduplicated.
        if not failed:
            state.data["mail_seen"] = sorted(set(state.data.get("mail_seen", [])) | set(pending_mail_hashes))
            if mail_success:
                state.data["mail_scan_after"] = (scan_start - timedelta(days=1)).isoformat()
        state.save()
    summary = (
        f"Mode={mode}; active sources={active}; candidate alerts={len(candidates)}; "
        f"sent={sent}; send failures={failed}; source failures={len(errors)}; "
        f"dry_run={dry_run}"
    )
    LOG.info(summary)
    for line in summaries:
        LOG.info("SOURCE COVERAGE: %s", line)
    report = audit.report()
    aggregate = {key: value for key, value in report.items() if key != "items"}
    LOG.info("AUDIT_SUMMARY: %s", json.dumps(aggregate, sort_keys=True))
    if dry_run:
        for row in report["items"]:
            LOG.info("AUDIT_ITEM: %s", json.dumps(row, sort_keys=True))
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write("## NZ Food Job Monitor\n\n" + summary + "\n\n")
            for line in summaries:
                f.write("- " + line + "\n")
            f.write("\n### Audit (counts; no job/mail content)\n\n```json\n")
            f.write(json.dumps(report if dry_run else aggregate, indent=2, sort_keys=True))
            f.write("\n```\n")
    if active == 0:
        LOG.warning("NO ACTIVE SOURCES. Configure Gmail OAuth/IMAP, Adzuna, or verified ATS sources.")
    return 1 if errors or failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="New Zealand Food Science job monitor")
    parser.add_argument("--mode", choices=("fast", "broad", "all"), default="fast")
    parser.add_argument("--config", default="config/sources.json")
    parser.add_argument("--state", default="state/seen.json")
    parser.add_argument("--dry-run", action="store_true", help="Print matches without sending alerts or writing state")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Third-party libraries must not write request URLs or account metadata.
    for name in ("urllib3", "requests", "google", "googleapiclient", "google_auth_httplib2", "oauthlib", "requests_oauthlib"):
        logging.getLogger(name).disabled = True
        logging.getLogger(name).addHandler(logging.NullHandler())
        logging.getLogger(name).propagate = False
    try:
        result = run(args.config, args.state, args.mode, args.dry_run)
    except Exception:
        LOG.error("Monitor stopped safely; exception details omitted for privacy")
        result = 1
    sys.exit(result)


if __name__ == "__main__":
    main()
