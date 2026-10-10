import base64
from urllib.parse import quote
from jobwatch.email_alerts import extract_jobs_from_html, identify_job_url, _extract_gmail_parts
from jobwatch.email_alerts import _imap_messages, _gmail_api_messages
from jobwatch.models import digest
from jobwatch.sources import SourceError
from unittest.mock import MagicMock
import pytest


def test_seek_linkedin_email_cards():
    email = """<html><div><h3>Food Technologist</h3><a href="https://nz.seek.com/job/12345?utm_source=email">Food Technologist at Good Foods</a></div>
    <div><h3>Postdoctoral Fellow Food Research</h3><a href="https://www.linkedin.com/comm/jobs/view/9876?trk=email">Postdoctoral Fellow Food Research</a></div>
    <a href="https://linkedin.com/help/linkedin">Help</a></html>"""
    found = extract_jobs_from_html(email)
    assert len(found) == 2
    assert any(x.company == "Good Foods" for x in found)
    assert all("utm_source" not in x.url and "trk=" not in x.url for x in found)


def test_redirects_and_security():
    link = "https://click.example.org/go?url=" + quote("https://nz.seek.com/job/555?utm_source=mail")
    result = identify_job_url(link)
    assert result and result[0] == "SEEK"
    assert identify_job_url("https://evil-linkedin.com/jobs/view/12345") is None
    assert identify_job_url("https://www.linkedin.com/unsubscribe") is None


def test_base64_gmail_parts():
    content = '<a href="https://www.linkedin.com/jobs/view/222">Food Scientist</a>'
    encoded = base64.urlsafe_b64encode(content.encode()).decode().rstrip("=")
    html, text = _extract_gmail_parts({"mimeType": "text/html", "body": {"data": encoded}})
    assert "Food Scientist" in html and not text


def test_imap_uidvalidity_prevents_reused_uid_being_skipped(monkeypatch):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.select.return_value = ('OK', [b'1'])
    connection.response.return_value = ('UIDVALIDITY', [b'new-epoch'])
    raw = b'From: alerts@seek.com\r\nSubject: New jobs\r\nContent-Type: text/plain\r\n\r\nFood Scientist\r\nhttps://nz.seek.com/job/1\r\n'
    connection.uid.side_effect = lambda op, *args: ('OK', [b'1']) if op == 'search' else ('OK', [(b'meta', raw)])
    monkeypatch.setenv('GMAIL_IMAP_USER', 'test@example.test')
    monkeypatch.setenv('GMAIL_IMAP_APP_PASSWORD', 'test-only')
    monkeypatch.setattr('jobwatch.email_alerts.imaplib.IMAP4_SSL', lambda *a: connection)
    old = digest('imap:test@example.test:old-epoch:1')
    jobs, hashes, count = _imap_messages({old})
    assert len(jobs) == count == 1 and old not in hashes
    assert _imap_messages(set(hashes)) == ([], [], 0)


def test_gmail_overflow_is_failed_instead_of_partial_baseline(monkeypatch):
    credentials = MagicMock(refresh_token='fake')
    service = MagicMock()
    messages = service.users.return_value.messages.return_value
    messages.list.return_value.execute.side_effect = [
        {'messages': [{'id': str(page * 100 + i)} for i in range(100)], 'nextPageToken': str(page + 1)}
        for page in range(5)
    ]
    messages.get.return_value.execute.return_value = {'payload': {}}
    monkeypatch.setenv('GMAIL_OAUTH_TOKEN_JSON', '{}')
    monkeypatch.setattr('google.oauth2.credentials.Credentials.from_authorized_user_info', lambda *a: credentials)
    monkeypatch.setattr('googleapiclient.discovery.build', lambda *a, **kw: service)
    with pytest.raises(SourceError, match='limit'):
        _gmail_api_messages(set())


def test_attached_html_is_not_treated_as_message_body():
    html, text = _extract_gmail_parts({'filename': 'attachment.html', 'mimeType': 'text/html', 'body': {'data': base64.urlsafe_b64encode(b'private attachment').decode()}})
    assert not html and not text
