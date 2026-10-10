import pytest
import requests

from jobwatch.notification import send_message


@pytest.fixture
def telegram(monkeypatch):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'private-test-token')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', 'private-test-chat')
    monkeypatch.setattr('jobwatch.notification.time.sleep', lambda _: None)


def test_network_error_is_redacted_and_retried(telegram, monkeypatch):
    attempts = []
    def fail(*a, **kw):
        attempts.append(1)
        raise requests.ConnectionError('https://api.telegram.org/botprivate-test-token')
    monkeypatch.setattr('jobwatch.notification.requests.post', fail)
    with pytest.raises(RuntimeError) as error:
        send_message('test')
    assert len(attempts) == 3
    assert 'private-test-token' not in str(error.value)


def test_rate_limit_then_success(telegram, monkeypatch):
    class Response:
        def __init__(self, status, body):
            self.status_code, self.body = status, body
        def json(self):
            return self.body
    replies = iter([Response(429, {'parameters': {'retry_after': -1}}), Response(200, {'ok': True})])
    monkeypatch.setattr('jobwatch.notification.requests.post', lambda *a, **kw: next(replies))
    send_message('test')


def test_invalid_json_is_recoverable(telegram, monkeypatch):
    class Response:
        status_code = 200
        def json(self):
            raise ValueError('private response data')
    monkeypatch.setattr('jobwatch.notification.requests.post', lambda *a, **kw: Response())
    with pytest.raises(RuntimeError, match='invalid JSON'):
        send_message('test')
