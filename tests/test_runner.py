import json
from pathlib import Path
from jobwatch.models import Job, SourceResult
from jobwatch.runner import run
from jobwatch.state import State
from jobwatch.notification import job_message
from jobwatch.matching import match_job


def test_first_run_baseline_then_new_job_only(tmp_path, monkeypatch):
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"adzuna_queries": [], "ats": []}))
    statefile = tmp_path / "state.json"
    entries = [Job("LinkedIn email alert", "111", "Food Scientist", "Fonterra", "Auckland", "Food product development", "https://www.linkedin.com/jobs/view/111")]
    monkeypatch.setattr("jobwatch.runner.gmail_alerts", lambda _: (SourceResult("gmail-alerts", list(entries), "ALERT_ONLY", "mocked"), ["hash-email-one"]))
    sent = []
    monkeypatch.setattr("jobwatch.runner.send_message", lambda s: sent.append(s))
    assert run(str(cfg), str(statefile), "fast") == 0
    assert sent == []
    assert State(statefile).initialized("gmail-alerts")
    entries.append(Job("LinkedIn email alert", "222", "NPD Food Technologist", "Fonterra", "Hamilton", "Dairy food formulation", "https://www.linkedin.com/jobs/view/222"))
    assert run(str(cfg), str(statefile), "fast") == 0
    assert len(sent) == 1
    assert run(str(cfg), str(statefile), "fast") == 0
    assert len(sent) == 1


def test_failed_notification_retried(tmp_path, monkeypatch):
    cfg = tmp_path / "sources.json"
    cfg.write_text(json.dumps({"adzuna_queries": [], "ats": []}))
    statefile = tmp_path / "state.json"
    entries = []
    monkeypatch.setattr("jobwatch.runner.gmail_alerts", lambda _: (SourceResult("gmail-alerts", list(entries), "ALERT_ONLY", "mocked"), []))
    assert run(str(cfg), str(statefile), "fast") == 0
    entries.append(Job("SEEK email alert", "100", "Food Technologist", "Company", "Auckland", "Food product development", "https://nz.seek.com/job/100"))
    sent = []
    def failed(text):
        if not sent:
            sent.append("failed")
            raise RuntimeError("Network offline")
        sent.append("ok")
    monkeypatch.setattr("jobwatch.runner.send_message", failed)
    assert run(str(cfg), str(statefile), "fast") == 1
    assert run(str(cfg), str(statefile), "fast") == 0
    assert sent == ["failed", "ok"]


def test_telegram_html_escaping():
    item = Job("unit", "12", "Food <Scientist>", "A&B", "Auckland", "food research", "https://example.com/job")
    rendered = job_message(item, match_job(item))
    assert "&lt;Scientist&gt;" in rendered
    assert "A&amp;B" in rendered
