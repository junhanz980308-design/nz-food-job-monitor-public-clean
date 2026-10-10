import json
import subprocess

import pytest

from scripts import state_branch


@pytest.fixture(autouse=True)
def local_git_environment(monkeypatch):
    from cryptography.fernet import Fernet
    monkeypatch.setenv("MONITOR_STATE_KEY", Fernet.generate_key().decode())


def command(*args, cwd):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_state_branch_migration_reload_and_competing_writer(tmp_path, monkeypatch):
    remote = tmp_path / 'remote.git'
    command('init', '--bare', str(remote), cwd=tmp_path)
    repo = tmp_path / 'repo'
    command('init', str(repo), cwd=tmp_path)
    command('config', 'user.email', 'test@example.test', cwd=repo)
    command('config', 'user.name', 'Test', cwd=repo)
    command('remote', 'add', 'origin', str(remote), cwd=repo)
    monkeypatch.chdir(repo)
    (repo / 'state').mkdir()
    seed = {'records': {}, 'aliases': {}, 'initialized_sources': {'source': True}}
    state_branch.STATE.write_text(json.dumps(seed))
    state_branch.load()
    state_branch.save()
    state_branch.load()
    original_parent = state_branch.PARENT.read_text()
    assert json.loads(state_branch.STATE.read_text()) == seed
    newer = dict(seed, mail_seen=['hash'])
    state_branch.STATE.write_text(json.dumps(newer))
    state_branch.save()
    # Simulate a second worker still based on the old snapshot.
    state_branch.PARENT.write_text(original_parent)
    state_branch.STATE.write_text(json.dumps(dict(seed, mail_seen=['other-hash'])))
    with pytest.raises(RuntimeError):
        state_branch.save()
    state_branch.load()
    assert json.loads(state_branch.STATE.read_text()) == newer
    files = command('ls-tree', '--name-only', 'FETCH_HEAD', cwd=repo)
    assert files == 'seen.enc'
    encrypted = command('show', 'FETCH_HEAD:seen.enc', cwd=repo)
    assert 'mail_seen' not in encrypted and 'other-hash' not in encrypted


def test_remote_failure_never_resets_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state_branch.STATE.parent.mkdir()
    state_branch.STATE.write_text('existing history')
    def fail(*args, **kwargs):
        raise RuntimeError('network error')
    monkeypatch.setattr(state_branch, 'git', fail)
    with pytest.raises(RuntimeError):
        state_branch.load()
    assert state_branch.STATE.read_text() == 'existing history'
