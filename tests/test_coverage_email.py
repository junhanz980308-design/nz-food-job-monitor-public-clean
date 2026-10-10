from urllib.parse import quote
import pytest
from jobwatch.email_alerts import (
    _email_to_jobs, new_mail_counters, identify_job_url, extract_jobs_from_html,
    extract_jobs_from_text, gmail_alerts, MailScanError,
)


@pytest.mark.parametrize('url,platform,ident', [
    ('https://nz.seek.com/job/123?recipient=private', 'SEEK', '123'),
    ('https://www.linkedin.com/jobs/view/food-scientist-at-example-456/?tracking=private', 'LinkedIn', '456'),
    ('https://www.linkedin.com/comm/jobs/view/456', 'LinkedIn', '456'),
    ('https://nz.indeed.com/rc/clk?jk=abc12&secret=private', 'Indeed', 'abc12'),
    ('https://nz.indeed.com/m/viewjob?jk=abc12', 'Indeed', 'abc12'),
    ('https://nz.indeed.com/pagead/clk?jk=abc12', 'Indeed', 'abc12'),
    ('https://www.trademe.co.nz/a/jobs/science/listing/789?token=private', 'Trade Me', '789'),
    ('https://www.trademe.co.nz/jobs/science/auction-789.htm?token=private', 'Trade Me', '789'),
])
def test_supported_patterns_and_offline_redirects(url, platform, ident):
    direct = identify_job_url(url)
    assert direct[:2] == (platform, ident)
    assert 'private' not in direct[2]
    wrapped = 'https://tracker.example.test/click?url=' + quote(url, safe='')
    twice = 'https://tracker.example.test/click?redirect=' + quote(wrapped, safe='')
    assert identify_job_url(wrapped) == direct == identify_job_url(twice)


@pytest.mark.parametrize('url', [
    'https://nz.seek.com/job/123garbage', 'https://www.linkedin.com/jobs/view/not-a-job',
    'https://indeed.com.evil.test/viewjob?jk=1', 'https://tracker.test/opaque/abc',
    'https://www.trademe.co.nz/a/motors/listing/789',
])
def test_unsupported_or_deceptive_links_stay_rejected(url):
    assert identify_job_url(url) is None


def test_mixed_mime_does_not_drop_plaintext_jobs_or_duplicate_html_jobs():
    counters = new_mail_counters()
    html = '<div><a href="https://nz.seek.com/job/1">Analytical Scientist</a><a href="https://nz.seek.com/job/1">View job</a></div>'
    text = 'Analytical Scientist\nhttps://nz.seek.com/job/1\nApplications Technologist https://nz.seek.com/job/2'
    jobs = _email_to_jobs('New jobs', 'SEEK <alerts@seek.com>', html, text, counters)
    assert {j.source_id for j in jobs} == {'1', '2'}
    assert counters['SEEK']['links_extracted'] == 2
    assert counters['SEEK']['emails_checked'] == counters['SEEK']['alerts_recognized'] == 1


def test_cards_do_not_borrow_titles_or_food_context_from_other_jobs():
    html = '''<div><article><h3>Food Scientist</h3><a href="https://nz.seek.com/job/1">View job</a></article>
    <article><a href="https://nz.seek.com/job/2">View job</a></article>
    <article><a href="https://nz.seek.com/job/3">Research Fellow</a></article></div>'''
    jobs = extract_jobs_from_html(html)
    assert [j.source_id for j in jobs] == ['1', '3']
    assert jobs[1].description == 'Research Fellow'


def test_counters_distinguish_no_email_unparsed_and_untrusted():
    c = new_mail_counters()
    _email_to_jobs('New jobs', 'alert@seek.com', '<a href="https://tracker.test/click/opaque">View jobs</a>', '', c)
    _email_to_jobs('Newsletter', 'alert@indeed.com', '<p>News</p>', '', c)
    _email_to_jobs('Jobs', 'SEEK seek.com <attacker@evil.test>', '<a href="https://nz.seek.com/job/1">Food Scientist</a>', '', c)
    assert c['SEEK']['emails_checked'] == c['SEEK']['alerts_recognized'] == c['SEEK']['parse_failures'] == c['SEEK']['unknown_coverage'] == 1
    assert c['Indeed']['emails_checked'] == c['Indeed']['unknown_coverage'] == 1
    assert c['Indeed']['alerts_recognized'] == 0
    assert c['LinkedIn']['emails_checked'] == 0
    assert c['Unknown']['unknown_coverage'] == 1
    assert 'attacker' not in str(c)


def test_title_subject_still_needs_trusted_sender_and_supported_job_link():
    body = 'Research Fellow\nhttps://nz.seek.com/job/1'
    assert len(_email_to_jobs('Research Fellow', 'alerts@seek.com', '', body)) == 1
    assert not _email_to_jobs('Research Fellow', 'seek.com@evil.test', '', body)


def test_partial_card_without_title_reports_unknown_coverage():
    c = new_mail_counters()
    _email_to_jobs('Jobs', 'alerts@seek.com', '<article><a href="https://nz.seek.com/job/1">Food Scientist</a></article><article><a href="https://nz.seek.com/job/2">View job</a></article>', '', c)
    assert c['SEEK']['links_extracted'] == c['SEEK']['unknown_coverage'] == 1


def test_transport_failure_is_unknown_not_zero_received(monkeypatch):
    monkeypatch.delenv('GMAIL_OAUTH_TOKEN_JSON', raising=False)
    monkeypatch.setenv('GMAIL_IMAP_USER', 'fake')
    monkeypatch.setenv('GMAIL_IMAP_APP_PASSWORD', 'fake')
    def fail(*args):
        raise ValueError('private@example.test secret https://private.test')
    monkeypatch.setattr('jobwatch.email_alerts._imap_messages', fail)
    with pytest.raises(MailScanError) as error:
        gmail_alerts({})
    assert all(c['coverage'] == 'scan_failed' for c in error.value.counters.values())
    assert 'private' not in str(error.value)


def test_parser_exception_is_counted_without_leaking_input(monkeypatch):
    counters = new_mail_counters()
    def fail(_):
        raise ValueError('private@example.test https://private.test/token')
    monkeypatch.setattr('jobwatch.email_alerts.extract_jobs_from_html', fail)
    with pytest.raises(Exception, match='Email parsing failed') as error:
        _email_to_jobs('New jobs', 'alerts@seek.com', 'private body', '', counters)
    assert counters['SEEK']['emails_checked'] == counters['SEEK']['alerts_recognized'] == counters['SEEK']['parse_failures'] == counters['SEEK']['unknown_coverage'] == 1
    assert 'private' not in str(error.value)


def test_explicit_company_line_is_retained_for_cross_source_deduplication():
    jobs = extract_jobs_from_html('<article><a href="https://nz.seek.com/job/1">Food Scientist</a><p>at Example Foods</p><p>Auckland</p></article>')
    assert jobs[0].company == 'Example Foods'
