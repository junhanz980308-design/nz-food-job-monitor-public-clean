"""All tests are offline, including mocked monitor runs."""
import imaplib
import socket
import pytest
import requests


@pytest.fixture(autouse=True)
def no_live_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Live network is forbidden during tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', blocked)
