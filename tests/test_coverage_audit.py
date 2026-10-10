import copy
import json
import logging
from pathlib import Path

import pytest
from jobwatch.models import Job, SourceResult
from jobwatch.runner import run, main
from jobwatch.state import State
from jobwatch.email_alerts import new_mail_counters


def job(ident, title='Food Scientist', description='Food research and dairy formulation'):
    return Job('SEEK email alert', str(ident), title, 'Example Foods', '', description,
               f'https://nz.seek.com/job/{ident}')


def report(caplog):
    lines = [r.message.split('AUDIT_SUMMARY: ', 1)[1] for r in caplog.records if 'AUDIT_SUMMARY: ' in r.message]
    return json.loads(lines[-1])


def test_fully_mocked_cli_dry_run_with_existing_history(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    cfg, history, summary = (tmp_path / name for name in ('config.json', 'history.json', 'summary.txt'))
    cfg.write_text(json.dumps({'adzuna_queries': ['food scientist'], 'ats': [{'type': 'smartrecruiters', 'name': 'Example'}], 'feeds': [{'name': 'Example'}]}))
    state = State(history)
    state.set_initialized('gmail-alerts')
    state.set_initialized('adzuna:food scientist')
    notified, _ = state.observe(job(1))
    state.mark_notified(notified)
    state.observe(job(2))  # Legacy unnotified record must not replay on new scoring.
    pending, _ = state.observe(job(3, 'NPD Technologist', ''))
    state.data['records'][pending]['alert_pending'] = True
    state.observe(job(99))  # Historical content not returned by any source.
    state.data['mail_seen'] = ['previous-mail-hash']
    state.data['mail_scan_after'] = '2026-01-01T00:00:00+00:00'
    state.save()
    original = history.read_bytes()
    fresh = job(4)
    records = [job(1), job(2), job(3, 'NPD Technologist', ''), fresh,
               job(5, 'Product Development Scientist', ''), job(6, 'Analytical Scientist', ''),
               job(7, 'Software Engineer', ''), job(8, '')]
    counts = new_mail_counters()
    counts['SEEK'].update(emails_checked=2, alerts_recognized=2, links_extracted=7, coverage='parsed')
    def mail(data):
        assert data['mail_seen'] == []  # Reinspect acknowledged email in audit only.
        return SourceResult('gmail-alerts', records, 'ALERT_ONLY', 'mocked', counts), ['new-mail-hash']
    monkeypatch.setattr('jobwatch.runner.gmail_alerts', mail)
    monkeypatch.setattr('jobwatch.runner.adzuna_search', lambda q: SourceResult('adzuna:' + q, [copy.deepcopy(fresh)]))
    monkeypatch.setattr('jobwatch.runner.poll_ats', lambda _: SourceResult('smartrecruiters:Example', [job(9)]))
    monkeypatch.setattr('jobwatch.runner.official_feed', lambda _: SourceResult('feed:Example', []))
    monkeypatch.setattr('jobwatch.runner.send_message', lambda _: pytest.fail('Telegram called during dry run'))
    monkeypatch.setattr(State, 'save', lambda _: pytest.fail('State.save called during dry run'))
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(summary))
    monkeypatch.setattr('sys.argv', ['monitor', '--config', str(cfg), '--state', str(history), '--mode', 'all', '--dry-run'])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    assert history.read_bytes() == original
    result = report(caplog)
    assert result['counts']['fetched'] == 10
    assert {k: result['counts'][k] for k in ['high_match', 'possible_match', 'ambiguous', 'low_match', 'invalid']} == dict(high_match=5, possible_match=2, ambiguous=1, low_match=1, invalid=1)
    assert result['counts']['duplicates'] == 1
    assert result['counts']['baseline_suppressed'] == 1
    assert result['counts']['already_notified'] == 1
    assert result['counts']['historical_suppressed'] == 1
    assert result['history_not_refetched'] == 1
    assert result['email_platforms']['SEEK']['emails_checked'] == 2
    output = caplog.text + summary.read_text()
    for private in ['Example Foods', 'https://nz.seek.com', 'previous-mail-hash', 'new-mail-hash']:
        assert private not in output
    assert 'context.insufficient_relevance' in output


def test_historical_reclassification_suppressed_but_new_failed_alert_retried(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    cfg, history = tmp_path / 'cfg.json', tmp_path / 'state.json'
    cfg.write_text('{}')
    state = State(history)
    state.set_initialized('gmail-alerts')
    old, _ = state.observe(job(1, 'Analytical Scientist', ''))
    old_record = copy.deepcopy(state.data['records'][old])
    state.save()
    entries = [job(1, 'Analytical Scientist', 'Food aroma research using chromatography'), job(2)]
    monkeypatch.setattr('jobwatch.runner.gmail_alerts', lambda _: (SourceResult('gmail-alerts', entries, 'ALERT_ONLY'), []))
    def fail(_):
        raise RuntimeError('mocked delivery failure')
    monkeypatch.setattr('jobwatch.runner.send_message', fail)
    assert run(str(cfg), str(history), 'fast') == 1
    assert report(caplog)['counts']['historical_suppressed'] == 1
    sent = []
    monkeypatch.setattr('jobwatch.runner.send_message', sent.append)
    assert run(str(cfg), str(history), 'fast') == 0
    assert len(sent) == 1
    assert State(history).data['records'][old] == old_record
    assert run(str(cfg), str(history), 'fast') == 0
    assert len(sent) == 1


def test_new_query_baseline_and_ambiguous_never_notify(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    cfg, history = tmp_path / 'cfg.json', tmp_path / 'state.json'
    cfg.write_text(json.dumps({'adzuna_queries': ['npd technologist']}))
    monkeypatch.setattr('jobwatch.runner.adzuna_search', lambda q: SourceResult('adzuna:' + q, [job(1), job(2, 'Research Fellow', '')]))
    monkeypatch.setattr('jobwatch.runner.send_message', lambda _: pytest.fail('baseline/ambiguous alert'))
    assert run(str(cfg), str(history), 'broad') == 0
    original = history.read_bytes()
    assert report(caplog)['counts']['baseline_suppressed'] == 2
    assert run(str(cfg), str(history), 'broad', dry_run=True) == 0
    assert report(caplog)['counts']['already_notified'] == 2
    assert history.read_bytes() == original


def test_ambiguous_new_job_only_appears_in_diagnostics(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    cfg, history = tmp_path / 'cfg.json', tmp_path / 'state.json'
    cfg.write_text('{}')
    state = State(history)
    state.set_initialized('gmail-alerts')
    state.save()
    monkeypatch.setattr('jobwatch.runner.gmail_alerts', lambda _: (SourceResult('gmail-alerts', [job(1, 'Research Fellow', '')], 'ALERT_ONLY'), []))
    monkeypatch.setattr('jobwatch.runner.send_message', lambda _: pytest.fail('ambiguous alert'))
    assert run(str(cfg), str(history), 'fast') == 0
    assert report(caplog)['counts']['ambiguous'] == 1


def test_adzuna_query_budget_and_existing_queries_preserved():
    queries = json.loads(
        Path("config/sources.json").read_text()
    )["adzuna_queries"]

    assert len(queries) == len(set(queries)) == 12

    assert {"food quality", "food safety"}.issubset(queries)

    assert len(queries) * 6 * 31 == 2232 < 2500


def test_failed_dry_run_preserves_health_and_history(tmp_path, monkeypatch, caplog):
    from jobwatch.email_alerts import MailScanError
    caplog.set_level(logging.INFO)
    cfg, history = tmp_path / 'cfg.json', tmp_path / 'state.json'
    cfg.write_text('{}')
    state = State(history)
    state.health_failed('gmail-alerts')
    state.health_failed('gmail-alerts')
    state.save()
    original = history.read_bytes()
    counters = new_mail_counters()
    for item in counters.values():
        item['coverage'] = 'scan_failed'
    def fail(_):
        raise MailScanError(counters)
    monkeypatch.setattr('jobwatch.runner.gmail_alerts', fail)
    monkeypatch.setattr('jobwatch.runner.send_message', lambda _: pytest.fail('health alert in dry run'))
    assert run(str(cfg), str(history), 'fast', dry_run=True) == 1
    assert history.read_bytes() == original
    assert report(caplog)['email_platforms']['SEEK']['coverage'] == 'scan_failed'
