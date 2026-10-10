import json

import pytest

from jobwatch.email_alerts import _is_job_alert, identify_job_url
from jobwatch.models import Job, SourceResult, canonical_url
from jobwatch.runner import run
from jobwatch.sources import SourceError, greenhouse, official_feed
from jobwatch.state import State


def test_sender_display_name_and_suffix_are_not_trusted():
    assert not _is_job_alert('New jobs', 'SEEK seek.com <spam@evil.test>')
    assert not _is_job_alert('New jobs', 'alert@seek.com.evil.test')
    assert _is_job_alert('New jobs', 'SEEK <alert@alerts.seek.com>')


@pytest.mark.parametrize('url', ['https://[bad/job/2', 'https://nz.seek.com:bad/job/2',
                                 'https://user:password@nz.seek.com/job/2'])
def test_malformed_or_credential_urls_are_rejected(url):
    assert identify_job_url(url) is None


def test_email_links_do_not_retain_personal_queries():
    result = identify_job_url('https://www.trademe.co.nz/a/jobs/science/listing/123?recipient=private&token=secret')
    assert result and '?' not in result[2]
    assert 'evil-linkedin.com' in canonical_url('https://evil-linkedin.com/jobs/view/3')


def test_real_mail_retry_reloads_unacknowledged_message(tmp_path, monkeypatch):
    cfg = tmp_path / 'config.json'
    cfg.write_text('{}')
    path = tmp_path / 'state.json'
    state = State(path)
    state.set_initialized('gmail-alerts')
    state.save()
    job = Job('SEEK email alert', '123', 'Food Scientist', url='https://nz.seek.com/job/123')
    def mail(data):
        unseen = 'message-hash' not in data.get('mail_seen', [])
        return SourceResult('gmail-alerts', [job] if unseen else [], 'ALERT_ONLY'), ['message-hash'] if unseen else []
    monkeypatch.setattr('jobwatch.runner.gmail_alerts', mail)
    def fail(_):
        raise RuntimeError('offline')
    monkeypatch.setattr('jobwatch.runner.send_message', fail)
    assert run(str(cfg), str(path), 'fast') == 1
    sent = []
    monkeypatch.setattr('jobwatch.runner.send_message', sent.append)
    assert run(str(cfg), str(path), 'fast') == 0
    assert len(sent) == 1
    assert run(str(cfg), str(path), 'fast') == 0
    assert len(sent) == 1


def test_same_employer_new_requisition_is_not_suppressed(tmp_path):
    state = State(tmp_path / 'state.json')
    a = Job('Adzuna', '1', 'Food Scientist', 'Good Foods', 'Auckland', url='https://adzuna.co.nz/details/1')
    b = Job('Adzuna', '2', 'Food Scientist', 'Good Foods', 'Auckland', url='https://adzuna.co.nz/details/2')
    first, _ = state.observe(a)
    second, fresh = state.observe(b)
    assert fresh and first != second


def test_invalid_success_payload_does_not_become_live(monkeypatch):
    monkeypatch.setattr('jobwatch.sources.get_json', lambda *a, **kw: {'error': 'invalid board'})
    with pytest.raises(SourceError):
        greenhouse('Employer', 'board', True)


def test_html_is_not_an_empty_live_feed(monkeypatch):
    class Response:
        status_code = 200
        content = b'<html><body>Careers</body></html>'
    monkeypatch.setattr('jobwatch.sources.requests.get', lambda *a, **kw: Response())
    with pytest.raises(SourceError):
        official_feed({'name': 'Employer', 'url': 'https://employer.test/jobs.xml'})


def test_dry_run_does_not_write_or_send(tmp_path, monkeypatch):
    cfg = tmp_path / 'config.json'
    cfg.write_text('{}')
    path = tmp_path / 'state.json'
    monkeypatch.setattr('jobwatch.runner.gmail_alerts', lambda _: (SourceResult('gmail-alerts', [], 'ALERT_ONLY'), ['hash']))
    monkeypatch.setattr('jobwatch.runner.send_message', lambda _: pytest.fail('dry run sent a message'))
    assert run(str(cfg), str(path), 'all', dry_run=True) == 0
    assert not path.exists()


def test_employer_scoped_ids_and_cross_source_merging(tmp_path):
    state = State(tmp_path / 'state.json')
    a = Job('Greenhouse', '1', 'Food Scientist', 'Employer A', 'Auckland', url='https://boards.greenhouse.io/a/jobs/1')
    b = Job('Greenhouse', '1', 'Food Scientist', 'Employer B', 'Auckland', url='https://boards.greenhouse.io/b/jobs/1')
    first, _ = state.observe(a)
    second, fresh = state.observe(b)
    assert fresh and first != second
    c = Job('Adzuna', '55', 'Food Scientist', 'Employer A', 'Auckland', url='https://adzuna.co.nz/details/55')
    assert state.observe(c) == (first, False)


def test_bridge_identity_preserves_notified_and_first_observed(tmp_path):
    state = State(tmp_path / 'state.json')
    a = Job('Adzuna', '1', 'Food Scientist', url='https://adzuna.co.nz/details/1')
    b = Job('SEEK email alert', '2', 'Food Scientist', url='https://nz.seek.com/job/2')
    first, _ = state.observe(a)
    second, _ = state.observe(b)
    state.data['records'][first]['first_seen'] = '2026-01-01T00:00:00+00:00'
    state.mark_notified(second)
    bridge = Job('Adzuna', '1', 'Food Scientist', url=b.url)
    merged, fresh = state.observe(bridge)
    assert merged == first and not fresh
    assert state.was_notified(merged)
    assert state.resolve(second) == first
    assert len(state.data['records']) == 1
