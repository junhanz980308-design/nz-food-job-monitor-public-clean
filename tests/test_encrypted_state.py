import json
from pathlib import Path
import subprocess

import pytest
from cryptography.fernet import Fernet
from scripts import state_branch
from jobwatch.state import State


@pytest.fixture
def history(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    for args in (("init", "--bare", str(remote)), ("init", str(repo))):
        subprocess.run(["git", *args], check=True, capture_output=True)
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.test")
    git("remote", "add", "origin", str(remote))
    monkeypatch.chdir(repo)
    monkeypatch.setenv("MONITOR_STATE_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")  # Works for public Actions.
    yield git


def test_new_public_repo_roundtrip_and_ciphertext_only(history):
    state_branch.load()
    before = state_branch.STATE.read_text()
    assert json.loads(before)["records"] == {}
    state_branch.save()
    commit = history("ls-remote", "origin", state_branch.BRANCH).split()[0]
    assert history("ls-tree", "-r", "--name-only", commit) == "seen.enc"
    encrypted = history("show", f"{commit}:seen.enc")
    assert encrypted != before and "records" not in encrypted
    state_branch.STATE.unlink()
    state_branch.load()
    assert state_branch.STATE.read_text() == before
    state_branch.save()
    assert history("ls-remote", "origin", state_branch.BRANCH).split()[0] == commit
    # No plaintext blob was ever passed to Git (including unreachable objects).
    objects = history("cat-file", "--batch-all-objects", "--batch-check=%(objectname) %(objecttype)")
    for entry in objects.splitlines():
        oid, kind = entry.split()
        if kind == "blob":
            assert "records" not in history("cat-file", "blob", oid)


@pytest.mark.parametrize("key", [None, "invalid"])
@pytest.mark.parametrize("operation", ["load", "save"])
def test_missing_key_never_touches_git(tmp_path, monkeypatch, key, operation):
    monkeypatch.chdir(tmp_path)
    if key is None:
        monkeypatch.delenv("MONITOR_STATE_KEY", raising=False)
    else:
        monkeypatch.setenv("MONITOR_STATE_KEY", key)
    monkeypatch.setattr(state_branch, "git", lambda *a, **k: pytest.fail("Git used without key"))
    with pytest.raises(RuntimeError, match="MONITOR_STATE_KEY"):
        getattr(state_branch, operation)()


def test_wrong_key_preserves_local_state_and_blocks_save(history, monkeypatch):
    state_branch.load()
    state_branch.save()
    original = state_branch.STATE.read_bytes()
    monkeypatch.setenv("MONITOR_STATE_KEY", Fernet.generate_key().decode())
    with pytest.raises(RuntimeError, match="authenticate"):
        state_branch.load()
    assert state_branch.STATE.read_bytes() == original
    assert not state_branch.PARENT.exists()
    with pytest.raises(FileNotFoundError):
        state_branch.save()


def test_plaintext_branch_refused(history):
    state_branch.load()
    blob = state_branch.git("hash-object", "-w", "--stdin", data='{"records":{},"aliases":{}}').stdout.strip()
    tree = state_branch.git("mktree", data=f"100644 blob {blob}\tseen.json\n").stdout.strip()
    commit = history("commit-tree", tree, "-m", "old plaintext")
    history("push", "origin", f"{commit}:{state_branch.BRANCH}")
    original = state_branch.STATE.read_bytes()
    with pytest.raises(RuntimeError, match="only encrypted"):
        state_branch.load()
    assert state_branch.STATE.read_bytes() == original


def test_tamper_rejected(history):
    state_branch.load()
    state_branch.save()
    parent = history("ls-remote", "origin", state_branch.BRANCH).split()[0]
    encrypted = state_branch.ENCRYPTED.read_text().strip()
    tampered = encrypted[:40] + ("A" if encrypted[40] != "A" else "B") + encrypted[41:]
    blob = state_branch.git("hash-object", "-w", "--stdin", data=tampered).stdout.strip()
    tree = state_branch.git("mktree", data=f"100644 blob {blob}\tseen.enc\n").stdout.strip()
    commit = history("commit-tree", tree, "-p", parent, "-m", "tamper")
    history("push", "origin", f"{commit}:{state_branch.BRANCH}")
    with pytest.raises(RuntimeError, match="authenticate"):
        state_branch.load()


def test_unknown_private_fields_never_saved(history):
    state_branch.load()
    data = json.loads(state_branch.STATE.read_text())
    data["credentials"] = "synthetic-not-real"
    state_branch.STATE.write_text(json.dumps(data))
    with pytest.raises(RuntimeError, match="Unexpected state"):
        state_branch.save()
    assert not state_branch.ENCRYPTED.exists()


def test_failed_push_leaves_encrypted_recovery(history, monkeypatch):
    state_branch.load()
    original = state_branch.STATE.read_bytes()
    real = state_branch.git
    def fail_push(*args, **kwargs):
        if args[0] == "push":
            raise RuntimeError("mocked push failure")
        return real(*args, **kwargs)
    monkeypatch.setattr(state_branch, "git", fail_push)
    with pytest.raises(RuntimeError):
        state_branch.save()
    assert state_branch.cipher().decrypt(state_branch.ENCRYPTED.read_bytes()) == original


def test_public_workflow_keeps_schedules_and_only_encrypted_artifact():
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/monitor.yml").read_text()
    assert "repository.private" not in workflow
    assert '"7,17,27,37,47,57 * * * *"' in workflow
    assert '"13 */4 * * *"' in workflow
    assert "secrets.MONITOR_STATE_KEY" in workflow
    assert "path: state/seen.enc" in workflow
    assert "path: state/seen.json" not in workflow
    assert "pull_request_target" not in workflow
