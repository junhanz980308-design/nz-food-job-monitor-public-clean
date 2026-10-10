import json
import logging
from pathlib import Path

from jobwatch.models import SourceResult
from jobwatch.sources import SourceError
from jobwatch.runner import run, main
import pytest


def test_untrusted_source_metadata_and_errors_are_not_logged(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    private = "synthetic-private-marker"
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"ats": [{"name": private}]}))
    summary = tmp_path / "summary.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr("jobwatch.runner.gmail_alerts", lambda _: (SourceResult(private, [], "ALERT_ONLY", private), []))
    def fail(_):
        raise SourceError(private)
    monkeypatch.setattr("jobwatch.runner.poll_ats", fail)
    assert run(str(config), str(tmp_path / "state.json"), "fast", dry_run=True) == 1
    assert private not in caplog.text + summary.read_text()


def test_fatal_errors_do_not_print_traceback_or_details(monkeypatch, caplog):
    def fail(*a, **k):
        raise ValueError("synthetic-private-marker")
    monkeypatch.setattr("jobwatch.runner.run", fail)
    monkeypatch.setattr("sys.argv", ["monitor"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 1
    assert "synthetic-private-marker" not in caplog.text
