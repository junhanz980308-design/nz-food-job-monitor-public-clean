"""Keep authenticated encrypted history on a separate branch; never overwrite a competing writer."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

BRANCH = "refs/heads/monitor-state"
PARENT = Path(".state-parent")
STATE = Path("state/seen.json")
ENCRYPTED = Path("state/seen.enc")


def git(*args: str, data: str | None = None, allowed=(0,)) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], input=data, text=True, capture_output=True)
    if result.returncode not in allowed:
        # Git errors may contain authenticated remote URLs; don't echo them.
        raise RuntimeError(f"State git operation failed: {args[0]} (exit {result.returncode})")
    return result


def cipher():
    from cryptography.fernet import Fernet
    try:
        return Fernet(os.environ["MONITOR_STATE_KEY"].encode("ascii"))
    except (KeyError, ValueError, UnicodeError):
        raise RuntimeError("Missing or invalid MONITOR_STATE_KEY") from None


def validate(content: str) -> dict:
    data = json.loads(content)
    allowed = {"version", "records", "aliases", "initialized_sources", "health", "mail_seen", "mail_scan_after"}
    if not isinstance(data, dict) or set(data) - allowed:
        raise RuntimeError("Unexpected state fields")
    if not isinstance(data.get("records"), dict) or not isinstance(data.get("aliases"), dict):
        raise RuntimeError("Invalid state; refusing to reset history")
    record_fields = {"first_seen", "first_source", "notified", "namespaces", "alert_pending"}
    for record in data["records"].values():
        if not isinstance(record, dict) or set(record) - record_fields:
            raise RuntimeError("Unexpected record fields")
    return data


def remote_content(parent: str, encryption) -> str:
    # Never build on a plaintext branch, even when a key is available.
    files = git("ls-tree", "-r", "--name-only", parent).stdout.splitlines()
    if files != ["seen.enc"]:
        raise RuntimeError("State branch must contain only encrypted history")
    encrypted = git("show", f"{parent}:seen.enc").stdout.strip().encode("ascii")
    try:
        content = encryption.decrypt(encrypted).decode("utf-8")
        validate(content)
        return content
    except Exception:
        raise RuntimeError("Cannot authenticate encrypted history; preserve branch and key") from None


def load() -> None:
    PARENT.unlink(missing_ok=True)
    encryption = cipher()
    found = git("ls-remote", "--exit-code", "--heads", "origin", BRANCH, allowed=(0, 2))
    if found.returncode == 2:
        # Preserve any local history; a new clone starts with an empty state.
        if not STATE.exists():
            from jobwatch.state import State
            State(STATE).save()
        PARENT.write_text("")
        print("No monitor-state branch yet; using local state (empty on a new clone).")
        return
    git("fetch", "--no-tags", "origin", BRANCH)
    parent = git("rev-parse", "FETCH_HEAD").stdout.strip()
    content = remote_content(parent, encryption)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(content, encoding="utf-8")
    PARENT.write_text(parent)


def save() -> None:
    encryption = cipher()
    parent = PARENT.read_text().strip()  # Missing load marker is a hard failure.
    content = STATE.read_text(encoding="utf-8")
    validate(content)
    if parent and remote_content(parent, encryption) == content:
        print("No state changes.")
        return
    encrypted = encryption.encrypt(content.encode("utf-8")).decode("ascii")
    ENCRYPTED.parent.mkdir(parents=True, exist_ok=True)
    ENCRYPTED.write_text(encrypted + "\n", encoding="ascii")
    blob = git("hash-object", "-w", "--stdin", data=encrypted + "\n").stdout.strip()
    tree = git("mktree", data=f"100644 blob {blob}\tseen.enc\n").stdout.strip()
    args = ["commit-tree", tree, "-m", "chore: persist encrypted monitoring history [skip ci]"]
    if parent:
        args.extend(["-p", parent])
    commit = git(*args).stdout.strip()
    # A regular fast-forward push rejects a newer remote parent. No force push.
    git("push", "origin", f"{commit}:{BRANCH}")
    print("Encrypted history saved to monitor-state.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("load", "save"))
    args = parser.parse_args()
    try:
        {"load": load, "save": save}[args.operation]()
    except (RuntimeError, ValueError, OSError) as exc:
        raise SystemExit(f"History {args.operation} failed ({type(exc).__name__}); inspect permissions/connectivity before retrying.") from None
