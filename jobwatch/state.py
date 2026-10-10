"""Private runtime history: hashes and timestamps still must not be published."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from .models import Job, digest, now_utc


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {}
        self.data.setdefault("version", 1)
        self.data.setdefault("records", {})
        self.data.setdefault("aliases", {})
        self.data.setdefault("initialized_sources", {})
        self.data.setdefault("health", {})
        self.redirects: dict[str, str] = {}

    def resolve(self, canonical_id: str) -> str:
        while canonical_id in self.redirects:
            canonical_id = self.redirects[canonical_id]
        return canonical_id

    def initialized(self, source_id: str) -> bool:
        return bool(self.data["initialized_sources"].get(source_id))

    def set_initialized(self, source_id: str) -> None:
        self.data["initialized_sources"][source_id] = True

    def observe(self, job: Job) -> tuple[str, bool]:
        tokens = job.tokens()
        aliases, records = self.data["aliases"], self.data["records"]
        strong = {aliases[t] for t in tokens if t.startswith(("id:", "url:")) and t in aliases}
        canonical_id = min(strong, key=lambda key: records[key]["first_seen"]) if strong else None
        if canonical_id:
            for other in strong - {canonical_id}:
                records[canonical_id]["notified"] |= records[other]["notified"]
                if records[other].get("alert_pending"):
                    records[canonical_id]["alert_pending"] = True
                records[canonical_id].setdefault("namespaces", []).extend(records[other].get("namespaces", []))
                for token, target in list(aliases.items()):
                    if target == other:
                        aliases[token] = canonical_id
                self.redirects[other] = canonical_id
                del records[other]
        if not canonical_id:
            for token in tokens:
                target = aliases.get(token)
                if not token.startswith("role:") or not target:
                    continue
                record = records[target]
                # A title is not a requisition ID. Never collapse distinct jobs
                # on the same board or a much later re-advertisement.
                namespaces = record.get("namespaces", [digest(record.get("first_source", ""))])
                recent = datetime.fromisoformat(now_utc()) - datetime.fromisoformat(record["first_seen"]) <= timedelta(days=14)
                if job.namespace() not in namespaces and recent:
                    canonical_id = target
                    break
        is_new = canonical_id is None
        if is_new:
            canonical_id = digest("|".join(sorted(tokens)))
            self.data["records"][canonical_id] = {
                "first_seen": now_utc(), "first_source": job.source, "notified": False
            }
        for token in tokens:
            self.data["aliases"][token] = canonical_id
        namespaces = records[canonical_id].setdefault("namespaces", [])
        if job.namespace() not in namespaces:
            namespaces.append(job.namespace())
        return canonical_id, is_new

    def was_notified(self, canonical_id: str) -> bool:
        return self.data["records"][canonical_id]["notified"]

    def mark_notified(self, canonical_id: str) -> None:
        self.data["records"][canonical_id]["notified"] = True
        self.data["records"][canonical_id].pop("alert_pending", None)

    def health_ok(self, source_id: str) -> None:
        self.data["health"][source_id] = {"failures": 0, "alert_sent": False}

    def health_failed(self, source_id: str) -> bool:
        status = self.data["health"].setdefault(source_id, {"failures": 0, "alert_sent": False})
        status["failures"] += 1
        return status["failures"] >= 3 and not status.get("alert_sent", False)

    def mark_health_alerted(self, source_id: str) -> None:
        self.data["health"][source_id]["alert_sent"] = True

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)
