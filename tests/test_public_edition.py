import json
from pathlib import Path
import subprocess

import pytest
from scripts import state_branch

ROOT = Path(__file__).resolve().parents[1]


def test_ignore_rules_block_runtime_and_credentials(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    private = [".env", ".env.production", ".secrets/gmail_token.json", "credentials.json", "client_secret_example.json", "token.json", "state/seen.json", "state/seen.tmp", "config/local.json", "logs/run.log", "private/export.json", ".state-parent", "key.pem"]
    result = subprocess.run(["git", "check-ignore", "--stdin"], cwd=tmp_path, input="\n".join(private), text=True, capture_output=True, check=True)
    assert set(result.stdout.splitlines()) == set(private)
    public = ["state/empty.example.json", "config/sources.json", "jobwatch/email_alerts.py", "tests/test_public_edition.py"]
    result = subprocess.run(["git", "check-ignore", "--stdin"], cwd=tmp_path, input="\n".join(public), text=True, capture_output=True)
    assert result.returncode == 1 and not result.stdout


def test_published_state_template_is_empty():
    data = json.loads((ROOT / "state/empty.example.json").read_text())
    assert data == dict(version=1, records={}, aliases={}, initialized_sources={}, health={}, mail_seen=[])
