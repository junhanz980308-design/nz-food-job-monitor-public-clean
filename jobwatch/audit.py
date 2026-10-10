"""Bounded, privacy-preserving decision reports; no job or mail content."""
from __future__ import annotations

from collections import Counter

TIER_KEYS = {"HIGH MATCH": "high_match", "POSSIBLE MATCH": "possible_match",
             "AMBIGUOUS": "ambiguous", "LOW MATCH": "low_match"}


class Audit:
    def __init__(self, state):
        self.initial_ids = set(state.data["records"])
        self.initial_notified = {key for key, record in state.data["records"].items() if record["notified"]}
        self.initial_pending = {key for key, record in state.data["records"].items() if record.get("alert_pending") and not record["notified"]}
        self.seen = set()
        self.observed_history = set()
        self.counts = Counter({key: 0 for key in (
            "fetched", "invalid", *TIER_KEYS.values(), "duplicates", "baseline_suppressed",
            "already_notified", "historical_suppressed", "eligible", "retry_pending",
        )})
        self.rows = []
        self.mail = {}

    def record(self, state, canonical_id, match, decision, baseline):
        self.counts[TIER_KEYS[match.tier]] += 1
        resolved_seen = {state.resolve(key) for key in self.seen}
        duplicate = canonical_id in resolved_seen
        self.counts["duplicates"] += int(duplicate)
        self.seen.add(canonical_id)
        historical = {key for key in self.initial_ids if state.resolve(key) == canonical_id}
        self.observed_history.update(historical)
        notified = bool(historical & self.initial_notified)
        self.counts["already_notified"] += int(notified)
        self.counts["baseline_suppressed"] += int(baseline)
        if decision in {"historical_suppressed", "eligible", "retry_pending"}:
            self.counts[decision] += 1
        if len(self.rows) < 50:
            self.rows.append(dict(item=self.counts["fetched"], tier=match.tier, score=match.score,
                                  signals=match.signals, decision=decision, duplicate=duplicate,
                                  baseline=baseline, already_notified=notified))

    def report(self):
        return dict(counts=dict(self.counts), history_records=len(self.initial_ids),
                    history_not_refetched=len(self.initial_ids - self.observed_history),
                    email_platforms=self.mail, items=self.rows,
                    items_omitted=max(0, sum(self.counts[k] for k in TIER_KEYS.values()) - len(self.rows)))
