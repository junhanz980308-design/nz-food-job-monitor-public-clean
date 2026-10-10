import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock
from urllib.parse import quote

import pytest
from jobwatch import email_alerts as mail
from jobwatch.models import Job, SourceResult, digest
from jobwatch.runner import run
from jobwatch.state import State
from scripts.backfill_alerts import backfill


@pytest.mark.parametrize("platform,sender,tracking,target", [
    ("SEEK", "alerts@seek.com.au", "https://click.email.seek.com.au/uni/ss/c?token=SYNTHETIC", "https://www.seek.co.nz/job/98765432?tracking=SYNTHETIC"),
    ("Indeed", "alerts@indeed.com", "https://nz.indeed.com/pagead/clk?ad=SYNTHETIC", "https://nz.indeed.com/viewjob?jk=abc123"),
    ("LinkedIn", "jobs@linkedin.com", "https://www.linkedin.com/redirect?opaque=SYNTHETIC", "https://www.linkedin.com/comm/jobs/view/7654321"),
])
def test_realistic_nested_table_tracker_templates(platform, sender, tracking, target, monkeypatch):
    calls = []
    monkeypatch.setattr(mail.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    def head(self, url, **kw):
        calls.append(url)
        assert kw == dict(allow_redirects=False, timeout=4)
        response = MagicMock(status_code=302, headers={"Location": target})
        return response
    monkeypatch.setattr(mail.requests.Session, "head", head)
    html = f'<table><tr><td><table><tr><td><span class="job-title">Food Development Scientist</span></td></tr><tr><td><a href="{tracking}">View job</a></td></tr></table></td></tr></table>'
    counters = mail.new_mail_counters()
    jobs, ack = mail._parse_email("Your new jobs", sender, html, "", counters, mail.RedirectResolver())
    assert len(jobs) == 1 and jobs[0].title == "Food Development Scientist" and ack
    assert calls == [tracking]  # Never fetch destination job page.
    assert counters[platform]["links_extracted"] == 1
    assert "SYNTHETIC" not in json.dumps(counters)


@pytest.mark.parametrize("href", [
    "https://www.linkedin.com/jobs/search/?currentJobId=123456",
    "https://www.linkedin.com/comm/jobs/view?jobId=123456",
    "https://nz.indeed.com/clk?jk=abc123",
    "https://nz.indeed.com/company/clk?jk=abc123",
    "https://tracker.example.test/click?url=" + quote(quote("https://nz.seek.com/job/123456", safe=""), safe=""),
])
def test_new_direct_and_double_encoded_patterns(href):
    assert mail.identify_job_url(href)


def test_aria_title_and_partial_unresolved_sibling():
    c = mail.new_mail_counters()
    html = '<table><tr><td><a href="https://nz.seek.com/job/1" aria-label="Analytical Scientist"><img></a></td></tr><tr><td><a href="https://vendor.example.test/click/opaque">View job</a></td></tr></table>'
    jobs, ack = mail._parse_email("New jobs", "alert@seek.com", html, "", c)
    assert len(jobs) == 1 and jobs[0].title == "Analytical Scientist" and not ack
    assert c["SEEK"]["unsupported_url_patterns"] == c["SEEK"]["parse_failures"] == 1
    assert c["SEEK"]["failure_reasons"]["redirect_unresolved"] == 1


@pytest.mark.parametrize("destination", [
    "http://www.seek.com/job/2", "https://evil.test/job/2", "https://127.0.0.1/click", "https://seek.com.evil.test/job/2",
    "https://user:password@seek.com/job/2", "https://www.seek.com:444/job/2", "https://www.linkedin.com/jobs/view/2",
    "https://www.seek.com/login", "https://www.seek.com/click/start",
])
def test_unsafe_redirect_destinations_never_requested(destination, monkeypatch):
    calls = []
    monkeypatch.setattr(mail.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    def head(self, url, **kw):
        calls.append(url)
        return MagicMock(status_code=302, headers={"Location": destination})
    monkeypatch.setattr(mail.requests.Session, "head", head)
    result, reason = mail.RedirectResolver().resolve("https://www.seek.com/click/start", "SEEK")
    assert result is None and len(calls) == 1


def test_private_dns_and_budget_fail_closed(monkeypatch):
    monkeypatch.setattr(mail.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    resolver = mail.RedirectResolver()
    assert resolver.resolve("https://www.seek.com/click/x", "SEEK")[1] == "redirect_not_allowed"
    resolver.remaining = 0
    assert resolver.resolve("https://www.seek.com/click/y", "SEEK")[1] == "redirect_budget"


@pytest.mark.parametrize("status", [200, 401, 403, 405, 429])
def test_no_get_or_access_control_fallback(status, monkeypatch):
    monkeypatch.setattr(mail.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    monkeypatch.setattr(mail.requests.Session, "head", lambda *a, **k: MagicMock(status_code=status, headers={}))
    assert mail.RedirectResolver().resolve("https://www.seek.com/click/x", "SEEK")[1] == "redirect_not_http"


@pytest.mark.parametrize("sender,subject", [("news@indeed.com", "Career advice newsletter"), ("updates@linkedin.com", "Recommended reading"), ("news@seek.com", "Job search tips")])
def test_marketing_is_skipped_not_failed(sender, subject):
    counters = mail.new_mail_counters()
    jobs, ack = mail._parse_email(subject, sender, '<p>Improve your profile</p><a href="https://www.indeed.com/preferences">Preferences</a>', "", counters)
    assert jobs == [] and ack
    assert sum(c["alerts_recognized"] for c in counters.values()) == 0
    assert sum(c["non_job_emails"] for c in counters.values()) == 1


def imap_mock(monkeypatch, bodies):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.select.return_value = ("OK", [b"1"])
    connection.response.return_value = ("UIDVALIDITY", [b"epoch"])
    def uid(operation, *args):
        if operation == "search":
            return "OK", [b" ".join(str(i).encode() for i in bodies)]
        return "OK", [(b"metadata", bodies[int(args[0])])]
    connection.uid.side_effect = uid
    monkeypatch.delenv("GMAIL_OAUTH_TOKEN_JSON", raising=False)
    monkeypatch.setenv("GMAIL_IMAP_USER", "synthetic")
    monkeypatch.setenv("GMAIL_IMAP_APP_PASSWORD", "synthetic")
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", lambda *a: connection)
    return connection


def message(subject, body):
    return f"From: alerts@seek.com\r\nSubject: {subject}\r\nContent-Type: text/plain\r\n\r\n{body}".encode()


def test_failed_and_partial_alerts_are_retried_but_marketing_acknowledged(monkeypatch):
    bodies = {1: message("New jobs", "Unrecognized content"), 2: message("Newsletter", "Hello"), 3: message("New jobs", "Food Scientist\nhttps://nz.seek.com/job/3")}
    imap_mock(monkeypatch, bodies)
    state = {}
    result, ack = mail.gmail_alerts(state)
    assert result.status == "PARTIAL" and len(result.jobs) == 1
    bad = digest("imap:synthetic:epoch:1")
    assert bad not in ack and bad in state["mail_retry"] and len(ack) == 2
    state["mail_seen"] = ack + [bad]  # Even an old wrong ack must not win over retry.
    result, ack2 = mail.gmail_alerts(state)
    assert result.counters["SEEK"]["emails_checked"] == 1 and ack2 == []
    bodies[1] = message("New jobs", "NPD Technologist\nhttps://nz.seek.com/job/1")
    result, ack3 = mail.gmail_alerts(state)
    assert result.status == "ALERT_ONLY" and ack3 == [bad] and not state["mail_retry"]


def test_partial_scan_fails_visibly_pins_cursor_and_preserves_other_ack(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    imap_mock(monkeypatch, {1: message("New jobs", "Unknown template"), 2: message("Newsletter", "Hello")})
    cfg, path = tmp_path / "config.json", tmp_path / "state.json"
    cfg.write_text('{}')
    state = State(path)
    state.data["mail_scan_after"] = "2026-01-01T00:00:00+00:00"
    state.save()
    assert run(str(cfg), str(path), "fast") == 1
    data = State(path).data
    assert data["mail_scan_after"] == "2026-01-01T00:00:00+00:00"
    assert len(data["mail_seen"]) == len(data["mail_retry"]) == 1
    assert not data["initialized_sources"].get("gmail-alerts")
    assert "source failures=1" in caplog.text
    assert "Unknown template" not in caplog.text


def test_backfill_is_silent_idempotent_and_dry_run_preserves_bytes(tmp_path, monkeypatch, capsys):
    path = tmp_path / "state.json"
    state = State(path)
    state.data["mail_seen"] = ["old-good", "old-bad"]
    state.set_initialized("adzuna:food scientist")
    state.save()
    original = path.read_bytes()
    def scan(data):
        assert data["mail_seen"] == [] and data["mail_scan_before"].startswith("2026-01-03")
        data["mail_retry"] = ["old-bad"]
        return SourceResult("gmail-alerts", [Job("SEEK email alert", "1", "Food Scientist", url="https://nz.seek.com/job/1")], "PARTIAL", counters=mail.new_mail_counters()), ["old-good"]
    monkeypatch.setattr("scripts.backfill_alerts.gmail_alerts", scan)
    monkeypatch.setattr("jobwatch.notification.send_message", lambda *a: pytest.fail("Backfill sent Telegram"))
    assert backfill(str(path), "2026-01-01", "2026-01-03") == 1
    assert path.read_bytes() == original
    assert backfill(str(path), "2026-01-01", "2026-01-03", False) == 1
    data = State(path).data
    assert data["mail_seen"] == ["old-good"] and data["mail_retry"] == ["old-bad"]
    assert all(r["notified"] for r in data["records"].values())
    assert data["initialized_sources"] == {"adzuna:food scientist": True}
    once = path.read_bytes()
    backfill(str(path), "2026-01-01", "2026-01-03", False)
    assert path.read_bytes() == once
    assert "https://" not in capsys.readouterr().out


def test_oauth_acknowledges_only_complete_or_non_job_messages(monkeypatch):
    import base64
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": "bad"}, {"id": "good"}]}
    def get(**kwargs):
        body = "Unknown" if kwargs["id"] == "bad" else "Food Scientist\nhttps://nz.seek.com/job/1"
        return MagicMock(execute=lambda: {"payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}, "headers": [{"name": "From", "value": "alerts@seek.com"}, {"name": "Subject", "value": "New jobs"}]}})
    messages.get.side_effect = get
    monkeypatch.setenv("GMAIL_OAUTH_TOKEN_JSON", "{}")
    monkeypatch.setattr("google.oauth2.credentials.Credentials.from_authorized_user_info", lambda *a: MagicMock(refresh_token="synthetic"))
    monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: service)
    retry = set()
    jobs, ack, checked = mail._gmail_api_messages(set(), counters=mail.new_mail_counters(), retry=retry)
    assert checked == 2 and len(jobs) == 1
    assert ack == [digest("gmail:good")] and retry == {digest("gmail:bad")}


def test_partial_html_ack_and_navigation_ignored(monkeypatch):
    counters = mail.new_mail_counters()
    retry = set()
    html = '<article><a href="https://nz.seek.com/job/1">Food Scientist</a></article><article><a href="https://nz.seek.com/job/2">View job</a></article><a href="https://external.test/click/unsubscribe">Unsubscribe</a>'
    jobs, ack = mail.read_message("New jobs", "alerts@seek.com", html, "", counters, None, "synthetic-hash", retry)
    assert len(jobs) == 1 and not ack and retry == {"synthetic-hash"}
    assert counters["SEEK"]["failure_reasons"] == {"missing_title": 1}


def test_unknown_opaque_email_not_silently_acknowledged():
    c = mail.new_mail_counters()
    _, ack = mail._parse_email("Your update", "alerts@indeed.com", '<a href="https://vendor.example.test/click/token">Details</a>', "", c)
    assert not ack and c["Indeed"]["alerts_recognized"] == 0
    assert c["Indeed"]["failure_reasons"]["uncertain_message"] == 1


def test_mixed_scan_failure_counters_are_finite_and_private(monkeypatch):
    resolver = mail.RedirectResolver()
    monkeypatch.setattr(mail.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(ValueError("SYNTHETIC-PRIVATE-CONTENT")))
    counters = mail.new_mail_counters()
    mail._parse_email("New jobs", "alerts@seek.com", '<a href="https://www.seek.com/click/opaque?secret=SYNTHETIC-PRIVATE-CONTENT">View job</a>', "", counters, resolver)
    output = json.dumps(counters)
    assert "SYNTHETIC-PRIVATE-CONTENT" not in output and "https://" not in output
    assert counters["SEEK"]["failure_reasons"]["redirect_request_failed"] == 1


def test_backfill_workflow_is_manual_silent_and_encrypted():
    root = __import__("pathlib").Path(__file__).resolve().parents[1]
    workflow = (root / ".github/workflows/backfill-alerts.yml").read_text()
    assert "workflow_dispatch:" in workflow and "schedule:" not in workflow
    assert "TELEGRAM_BOT_TOKEN" not in workflow and "ADZUNA_APP_KEY" not in workflow
    assert "path: state/seen.enc" in workflow and "path: state/seen.json" not in workflow
    assert "group: nz-food-job-monitor" in workflow and "default: false" in workflow


def test_pending_retry_cannot_be_reported_as_healthy_empty_scan(monkeypatch):
    monkeypatch.delenv("GMAIL_OAUTH_TOKEN_JSON", raising=False)
    monkeypatch.setenv("GMAIL_IMAP_USER", "synthetic")
    monkeypatch.setenv("GMAIL_IMAP_APP_PASSWORD", "synthetic")
    monkeypatch.setattr(mail, "_imap_messages", lambda *a: ([], [], 0))
    result, ack = mail.gmail_alerts({"mail_retry": ["synthetic-hash"]})
    assert result.status == "PARTIAL" and not ack
