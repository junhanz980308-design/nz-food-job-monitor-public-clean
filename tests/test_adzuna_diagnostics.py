"""Offline Adzuna failures: fixed labels only, no sensitive exception data."""
import json
import logging
from unittest.mock import Mock

import pytest
import requests

from jobwatch import runner, sources
from jobwatch.state import State

PRIVATE = "synthetic-private-content person@example.test https://example.test/?app_key=synthetic-secret Authorization: Bearer synthetic-token"


@pytest.fixture(autouse=True)
def synthetic_credentials(monkeypatch):
    monkeypatch.setenv("ADZUNA_APP_ID", "synthetic-app-id")
    monkeypatch.setenv("ADZUNA_APP_KEY", "synthetic-app-key")


def response(status=200, payload=None, json_error=None):
    result = Mock(status_code=status)
    result.url = "https://example.test/?app_key=synthetic-app-key"
    result.headers = {"Authorization": PRIVATE, "Retry-After": PRIVATE}
    result.json = Mock(return_value=payload, side_effect=json_error)
    # HTTP diagnostics must not inspect bodies or response headers.
    return result


@pytest.mark.parametrize("status,category", [
    (400, "http_400"), (401, "http_401"), (403, "http_403"), (429, "http_429"),
    (500, "http_5xx"), (502, "http_5xx"), (503, "http_5xx"), (599, "http_5xx"),
    (404, "http_other"), (301, "http_other"),
])
def test_http_status_is_categorized_without_body_or_retries(monkeypatch, status, category):
    result = response(status, json_error=AssertionError("HTTP error body must not be parsed"))
    request = Mock(return_value=result)
    monkeypatch.setattr(sources.requests, "get", request)
    with pytest.raises(sources.SourceError) as caught:
        sources.adzuna_search(PRIVATE)
    assert sources.source_error_category(caught.value) == category
    assert PRIVATE not in str(caught.value)
    request.assert_called_once()
    result.json.assert_not_called()


@pytest.mark.parametrize("exception,category", [
    (requests.exceptions.Timeout(PRIVATE), "network_timeout"),
    (requests.exceptions.ConnectTimeout(PRIVATE), "network_timeout"),
    (requests.exceptions.ReadTimeout(PRIVATE), "network_timeout"),
    (requests.exceptions.ConnectionError(PRIVATE), "dns_or_connection_error"),
    (requests.exceptions.SSLError(PRIVATE), "dns_or_connection_error"),
    (requests.exceptions.TooManyRedirects(PRIVATE), "network_error"),
    (requests.exceptions.RequestException(PRIVATE), "network_error"),
])
def test_transport_category_uses_type_not_exception_text(monkeypatch, exception, category):
    request = Mock(side_effect=exception)
    monkeypatch.setattr(sources.requests, "get", request)
    with pytest.raises(sources.SourceError) as caught:
        sources.adzuna_search(PRIVATE)
    assert sources.source_error_category(caught.value) == category
    assert PRIVATE not in str(caught.value) and caught.value.__suppress_context__
    request.assert_called_once()


@pytest.mark.parametrize("exception", [
    ValueError(PRIVATE),
    json.JSONDecodeError(PRIVATE, PRIVATE, 0),
    requests.exceptions.JSONDecodeError(PRIVATE, PRIVATE, 0),
])
def test_invalid_json_is_not_misclassified_as_network_error(monkeypatch, exception):
    request = Mock(return_value=response(json_error=exception))
    monkeypatch.setattr(sources.requests, "get", request)
    with pytest.raises(sources.SourceError) as caught:
        sources.adzuna_search(PRIVATE)
    assert sources.source_error_category(caught.value) == "invalid_json"
    assert PRIVATE not in str(caught.value) and caught.value.__suppress_context__
    request.assert_called_once()


@pytest.mark.parametrize("payload", [
    None, [], PRIVATE, {"error": PRIVATE}, {"results": {}}, {"results": [PRIVATE]},
    {"results": [None]}, {"results": [{"title": 123}]},
    {"results": [{"description": [PRIVATE]}]}, {"results": [{"company": PRIVATE}]},
    {"results": [{"company": {"display_name": [PRIVATE]}}]},
    {"results": [{"location": [PRIVATE]}]},
    {"results": [{"location": {"display_name": 123}}]},
    {"results": [{"salary_min": PRIVATE}]}, {"results": [{"salary_max": True}]},
    {"results": [{"redirect_url": {"private": PRIVATE}}]},
    {"results": [{"created": [PRIVATE]}]},
])
def test_unexpected_response_shapes_are_fixed_errors(monkeypatch, payload):
    request = Mock(return_value=response(payload=payload))
    monkeypatch.setattr(sources.requests, "get", request)
    with pytest.raises(sources.SourceError) as caught:
        sources.adzuna_search(PRIVATE)
    assert sources.source_error_category(caught.value) == "unexpected_response"
    assert PRIVATE not in str(caught.value)
    request.assert_called_once()


def test_valid_response_and_request_options_are_unchanged(monkeypatch):
    payload = {"results": [{"id": "1", "title": "Food Scientist", "description": "<p>Food research</p>",
                            "company": {"display_name": "Example Foods"}, "location": {"display_name": "Auckland"},
                            "redirect_url": "https://example.test/jobs/1", "salary_min": 70000, "salary_max": 90000.0}]}
    request = Mock(return_value=response(payload=payload))
    monkeypatch.setattr(sources.requests, "get", request)
    result = sources.adzuna_search("food scientist")
    assert result.status == "LIVE" and len(result.jobs) == 1
    job = result.jobs[0]
    assert (job.title, job.company, job.location, job.description) == ("Food Scientist", "Example Foods", "Auckland", "Food research")
    assert job.salary == "NZD 70,000–90,000 / year (Adzuna may estimate)"
    request.assert_called_once_with(
        "https://api.adzuna.com/v1/api/jobs/nz/search/1",
        params={"app_id": "synthetic-app-id", "app_key": "synthetic-app-key", "what": "food scientist",
                "results_per_page": 50, "max_days_old": 7, "sort_by": "date", "content-type": "application/json"},
        headers={"Accept": "application/json", "User-Agent": sources.USER_AGENT}, timeout=(8, 25))


def test_empty_valid_results_are_successful_and_missing_credentials_do_not_request(monkeypatch):
    request = Mock(return_value=response(payload={"results": []}))
    monkeypatch.setattr(sources.requests, "get", request)
    assert sources.adzuna_search("food scientist").status == "LIVE"
    monkeypatch.delenv("ADZUNA_APP_KEY")
    assert sources.adzuna_search("food scientist").status == "PENDING"
    request.assert_called_once()


@pytest.mark.parametrize("error", [
    RuntimeError(PRIVATE), sources.SourceError(PRIVATE),
    sources.SourceError(PRIVATE, category=PRIVATE),
    sources.SourceError(PRIVATE, category=[PRIVATE]),
])
def test_unknown_exceptions_and_untrusted_categories_cannot_leak(error, tmp_path, monkeypatch, caplog):
    cfg, summary = tmp_path / "config.json", tmp_path / "summary.md"
    cfg.write_text(json.dumps({"adzuna_queries": [PRIVATE]}))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(runner, "adzuna_search", Mock(side_effect=error))
    monkeypatch.setattr(runner, "send_message", lambda _: pytest.fail("Telegram called"))
    caplog.set_level(logging.INFO)
    assert runner.run(str(cfg), str(tmp_path / "unused.json"), "broad", dry_run=True) == 1
    output = caplog.text + summary.read_text()
    assert "query_index=1; category=unexpected_error" in output
    assert PRIVATE not in output and "example.test" not in output
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("category", sorted(sources.ERROR_CATEGORIES))
def test_all_safe_categories_reach_logs_and_summary_without_exception_details(category, tmp_path, monkeypatch, caplog):
    cfg, summary = tmp_path / "config.json", tmp_path / "summary.md"
    cfg.write_text(json.dumps({"adzuna_queries": [PRIVATE]}))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(runner, "adzuna_search", Mock(side_effect=sources.SourceError(PRIVATE, category=category)))
    caplog.set_level(logging.INFO)
    assert runner.run(str(cfg), str(tmp_path / "unused.json"), "broad", dry_run=True) == 1
    safe = f"Adzuna source failed; query_index=1; category={category}"
    assert safe in caplog.text and safe in summary.read_text()
    assert PRIVATE not in caplog.text + summary.read_text()


def test_twelve_queries_continue_after_five_failures_without_extra_requests_or_private_output(tmp_path, monkeypatch, caplog):
    queries = [f"synthetic-private-query-{i}" for i in range(1, 13)]
    cfg, history, summary = (tmp_path / name for name in ("config.json", "state.json", "summary.md"))
    cfg.write_text(json.dumps({"adzuna_queries": queries}))
    state = State(history)
    state.data.update(mail_seen=["synthetic-existing-ack"], mail_retry=["synthetic-existing-retry"])
    # Reach the existing health-notification threshold, but dry-run must not send.
    for query in queries[:5]:
        state.health_failed("adzuna:" + query)
        state.health_failed("adzuna:" + query)
    state.save()
    original = history.read_bytes()
    replies = [response(status=s) for s in (400, 401, 403, 429, 503)]
    for i, count in enumerate((12, 12, 12, 11, 11, 11, 11), start=6):
        replies.append(response(payload={"results": [
            {"id": f"{i}-{j}", "title": "Food Scientist " + PRIVATE, "description": PRIVATE,
             "company": {"display_name": PRIVATE}, "location": {"display_name": "Auckland"},
             "redirect_url": f"https://example.test/jobs/{i}-{j}?token=synthetic-private-token"}
            for j in range(count)]}))
    request = Mock(side_effect=replies)
    monkeypatch.setattr(sources.requests, "get", request)
    monkeypatch.setattr(runner, "gmail_alerts", lambda _: pytest.fail("Gmail called in broad mode"))
    monkeypatch.setattr(runner, "send_message", lambda _: pytest.fail("Telegram called"))
    monkeypatch.setattr(State, "save", lambda _: pytest.fail("History saved in dry run"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr("sys.argv", ["monitor", "--mode", "broad", "--dry-run", "--config", str(cfg), "--state", str(history)])
    caplog.set_level(logging.INFO)
    with pytest.raises(SystemExit) as exit_info:
        runner.main()
    assert exit_info.value.code == 1
    assert request.call_count == 12
    assert [call.kwargs["params"]["what"] for call in request.call_args_list] == queries
    assert history.read_bytes() == original
    assert "active sources=7; candidate alerts=0; sent=0; send failures=0; source failures=5; dry_run=True" in caplog.text
    audit = json.loads(next(r.message.split("AUDIT_SUMMARY: ")[1] for r in caplog.records if "AUDIT_SUMMARY: " in r.message))
    assert audit["counts"]["fetched"] == audit["counts"]["baseline_suppressed"] == 80
    output = caplog.text + summary.read_text()
    for index, category in enumerate(("http_400", "http_401", "http_403", "http_429", "http_5xx"), start=1):
        assert f"query_index={index}; category={category}" in output
    for private in (*queries, PRIVATE, "synthetic-app-id", "synthetic-app-key", "Authorization", "Bearer", "https://", "example.test", "synthetic-existing-ack", "synthetic-existing-retry"):
        assert private not in output
    assert all(record.exc_info is None for record in caplog.records)
