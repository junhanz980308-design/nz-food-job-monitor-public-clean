"""Synthetic fixtures only. Never load private/ or real mailbox data in tests."""
import json
import logging
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from jobwatch import email_alerts as mail
from jobwatch import runner
from jobwatch.models import Job, SourceResult
from jobwatch.state import State

TEMPLATE = (Path(__file__).parent / "fixtures/seek_linked_table.html").read_text()


def card(href, title="Food Scientist", company="Example Foods", location="Example City", salary="Synthetic salary range"):
    return TEMPLATE.format(href=href, title=title, company=company, location=location, salary=salary)


def plain(href, title="Food Scientist", company="Example Foods", location="Example City", salary="Synthetic salary range"):
    return f"{title}\n{company}\n\n{location}\n{salary}\n\n[{href}]\n"


def parse(html, text="", resolver=None):
    counters = mail.new_mail_counters()
    jobs, ack = mail._parse_email("New jobs", "alerts@seek.com", html, text, counters, resolver)
    return jobs, ack, counters["SEEK"]


@pytest.fixture
def network(monkeypatch):
    calls, targets = [], {}
    monkeypatch.setattr(mail.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    def head(self, url, **kwargs):
        calls.append(url)
        assert kwargs == dict(allow_redirects=False, timeout=4)
        assert not self.trust_env
        return MagicMock(status_code=302, headers={"Location": targets[url]})
    monkeypatch.setattr(mail.requests.Session, "head", head)
    return calls, targets


def test_alternate_mime_canonical_ids_avoid_every_redirect_and_match_by_content():
    titles = ["Food Scientist", "Baker", "Laboratory Quality Lead"]
    html = ''.join(card(f"https://www.seek.com/uni/ss/c/synthetic-{i}", t, f"Example Company {i}") for i, t in enumerate(titles, 1))
    text = ''.join(plain(f"https://nz.seek.com/job/{i}", t, f"Example Company {i}") for i, t in reversed(list(enumerate(titles, 1))))
    jobs, ack, c = parse(html, text, mail.RedirectResolver())
    assert ack and len(jobs) == 3
    assert {j.source_id: (j.title, j.company) for j in jobs} == {str(i): (t, f"Example Company {i}") for i, t in enumerate(titles, 1)}
    assert c["canonical_card_reuse"] == c["mime_card_reuse"] == c["structured_job_cards"] == 3
    assert c["unresolved_job_cards"] == c["redirect_requests"] == c["ambiguous_targets"] == 0
    assert not c["failure_reasons"]


@pytest.mark.parametrize("title", ["Food Scientist", "Chef", "Laboratory Quality Lead", "X" * 125])
def test_nested_styled_title_is_not_combined_with_metadata(title):
    html = card("https://nz.seek.com/job/101", title, "Example Company With Long Name", salary="A long synthetic salary description")
    jobs, ack, _ = parse(html)
    assert ack and len(jobs) == 1
    assert jobs[0].title == title and jobs[0].company == "Example Company With Long Name"


def test_title_and_company_must_both_match_uniquely():
    html = card("https://www.seek.com/click/one", company="Example One") + card("https://www.seek.com/click/two", company="Example Two")
    jobs, ack, c = parse(html, plain("https://nz.seek.com/job/202", company="Example Two"))
    assert len(jobs) == 1 and jobs[0].source_id == "202" and jobs[0].company == "Example Two"
    assert not ack and c["unresolved_job_cards"] == 1
    assert c["canonical_card_reuse"] == 1


def test_identical_title_company_cards_are_not_paired_by_position():
    html = card("https://www.seek.com/click/one") + card("https://www.seek.com/click/two")
    jobs, ack, c = parse(html, plain("https://nz.seek.com/job/202"))
    assert not ack and not jobs and c["canonical_card_reuse"] == 0
    assert c["unresolved_job_cards"] == 2 and c["failure_reasons"]["missing_title"] == 1


def test_two_text_identities_for_same_card_remain_conflicting():
    html = card("https://www.seek.com/click/one")
    text = plain("https://nz.seek.com/job/101") + plain("https://nz.seek.com/job/202")
    jobs, ack, c = parse(html, text, mail.RedirectResolver())
    assert not jobs and not ack
    assert c["failure_reasons"]["ambiguous_card_identity"] == 1
    assert c["unresolved_job_cards"] == 1 and c["redirect_requests"] == 0


def test_canonical_html_cannot_be_overwritten_by_conflicting_plaintext():
    jobs, ack, c = parse(card("https://nz.seek.com/job/101"), plain("https://nz.seek.com/job/202"), mail.RedirectResolver())
    assert not ack and [j.source_id for j in jobs] == ["101"]
    assert c["failure_reasons"]["ambiguous_card_identity"] == 1
    assert c["redirect_requests"] == 0


def test_unknown_neighbor_never_borrows_previous_text_title_or_identity():
    html = card("https://www.seek.com/click/one") + card("https://www.seek.com/click/two", "Dairy Technologist", "Example Dairy")
    text = plain("https://nz.seek.com/job/101") + "\n[https://nz.seek.com/job/202]"
    jobs, ack, c = parse(html, text)
    assert [j.source_id for j in jobs] == ["101"] and not ack
    assert c["unresolved_job_cards"] == 1 and c["failure_reasons"]["missing_title"] == 1


def test_generic_titles_have_strong_structural_evidence_for_resolution(network):
    calls, targets = network
    href = "https://www.seek.com/uni/ss/c/synthetic-card"
    targets[href] = "https://nz.seek.com/job/101"
    jobs, ack, c = parse(card(href, "Baker"), plain("https://www.seek.com/ss/c/synthetic-text", "Baker"), mail.RedirectResolver())
    assert ack and len(jobs) == 1 and jobs[0].title == "Baker"
    assert calls == [href] and c["canonical_card_reuse"] == 1


def test_failed_card_does_not_spend_budget_again_on_different_mime_tracker(network):
    calls, targets = network
    href = "https://www.seek.com/uni/ss/c/synthetic-card"
    targets[href] = "https://external.example.test/login"
    jobs, ack, c = parse(card(href), plain("https://www.seek.com/ss/c/synthetic-text"), mail.RedirectResolver())
    assert not jobs and not ack and calls == [href]
    assert c["unresolved_job_cards"] == 1 and c["redirect_requests"] == 1
    assert c["failure_reasons"]["reject_external_domain"] == c["failure_reasons"]["card_resolution_deferred"] == 1


def test_no_identity_is_invented_for_opaque_only_cards():
    jobs, ack, c = parse(card("https://www.seek.com/uni/ss/c/synthetic-card"), plain("https://www.seek.com/ss/c/synthetic-text"))
    assert not jobs and not ack and c["unresolved_job_cards"] == 1
    assert not c["ambiguous_targets"] and c["failure_reasons"]["resolution_unavailable"] == 1


def test_navigation_marketing_images_and_inline_image_links_do_not_defer():
    controls = ["SEEK", "Apple Store", "Google Play", "Edit this alert", "Unsubscribe from this alert", "View all 123 matching jobs", "Explore now", "Tips for making your saved search results relevant", "nz.seek.com", "au.seek.com"]
    html = card("https://www.seek.com/click/job") + ''.join(f'<a href="https://www.seek.com/click/nav{i}">{label}</a>' for i, label in enumerate(controls))
    html += '<img src="https://images.example.test/banner.png" alt="Synthetic recommendation banner">'
    text = "Logo\n[https://www.seek.com/click/logo]\n" + plain("https://nz.seek.com/job/101")
    text += "\nSynthetic recommendation banner\n[https://www.seek.com/click/banner]\n"
    text += "\nGoogle Play\n[https://www.seek.com/click/image]https://www.seek.com/click/store\n"
    text += "\nYou received this email after registering on nz.seek.com\n[https://www.seek.com/click/home]\n"
    jobs, ack, c = parse(html, text, mail.RedirectResolver())
    assert ack and len(jobs) == 1 and c["redirect_requests"] == 0
    assert c["link_decisions"]["decorative_image"] == 3
    assert c["link_decisions"]["marketing"] == 2 and not c["failure_reasons"]


def test_unexplained_footer_tracker_is_still_deferred():
    html = card("https://nz.seek.com/job/101") + '<a href="https://www.seek.com/click/unknown">Details</a>'
    jobs, ack, c = parse(html)
    assert len(jobs) == 1 and not ack and c["ambiguous_targets"] == 1


def test_brackets_image_pairs_and_duplicate_urls_have_exact_boundaries():
    text = "Food Scientist\n[https://nz.seek.com/job/101]\nFood Scientist\n[https://nz.seek.com/job/101]\nGoogle Play\n[https://images.example.test/icon]https://www.seek.com/click/store"
    links = list(mail.text_links(text))
    assert len(links) == 4 and all(']' not in t['href'] for t in links)
    assert links[-2]['image'] and links[-1]['label'] == "Google Play"
    jobs, ack, c = parse("", text)
    assert ack and len(jobs) == 1 and c["duplicate_links"] == 1


def test_missing_title_is_not_filled_from_employer_or_neighbor():
    html = card("https://nz.seek.com/job/101") + '<article><a href="https://nz.seek.com/job/202">View job</a></article>'
    jobs, ack, c = parse(html)
    assert not ack and len(jobs) == 1 and c["failure_reasons"]["missing_title"] == 1


def test_mocked_multipart_imap_ack_retry_and_existing_seen_are_preserved(monkeypatch):
    message = EmailMessage()
    message["From"], message["Subject"] = "alerts@seek.com", "New jobs"
    message.set_content(plain("https://nz.seek.com/job/101"))
    message.add_alternative(card("https://www.seek.com/click/one"), subtype="html")
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.select.return_value = ("OK", [b"1"])
    connection.response.return_value = ("UIDVALIDITY", [b"synthetic"])
    connection.uid.side_effect = lambda op, *a: ("OK", [b"1"]) if op == "search" else ("OK", [(b"synthetic", message.as_bytes())])
    monkeypatch.delenv("GMAIL_OAUTH_TOKEN_JSON", raising=False)
    monkeypatch.setenv("GMAIL_IMAP_USER", "synthetic")
    monkeypatch.setenv("GMAIL_IMAP_APP_PASSWORD", "synthetic")
    monkeypatch.setattr(mail.imaplib, "IMAP4_SSL", lambda *a: connection)
    from jobwatch.models import digest
    hashed = digest("imap:synthetic:synthetic:1")
    state = {"mail_seen": ["older-ack", hashed], "mail_retry": [hashed]}
    result, ack = mail.gmail_alerts(state)
    assert len(result.jobs) == 1 and ack == [hashed] and result.status == "ALERT_ONLY"
    assert state["mail_seen"] == ["older-ack", hashed] and state["mail_retry"] == []
    again, ack = mail.gmail_alerts(state)
    assert not again.jobs and not ack
    unresolved = card("https://www.seek.com/click/other", "Baker", "Example Bakery")
    retry = {"older-retry"}
    _, ok = mail.read_message("New jobs", "alerts@seek.com", unresolved, "", mail.new_mail_counters(), None, hashed, retry)
    assert not ok and retry == {"older-retry", hashed}


def test_mocked_dry_run_preserves_state_and_improved_titles_do_not_resend(tmp_path, monkeypatch, caplog):
    jobs, ack, counters = parse(card("https://www.seek.com/click/synthetic-secret"), plain("https://nz.seek.com/job/101"))
    assert ack
    path, config = tmp_path / "state.json", tmp_path / "config.json"
    config.write_text('{}')
    state = State(path)
    state.set_initialized("gmail-alerts")
    old = Job("SEEK email alert", "101", "Old combined title and metadata", url="https://nz.seek.com/job/101")
    key, _ = state.observe(old)
    state.mark_notified(key)
    state.data.update(mail_seen=["previous-ack"], mail_retry=["previous-retry"])
    state.save()
    original = path.read_bytes()
    monkeypatch.setattr(runner, "gmail_alerts", lambda *a: (SourceResult("gmail-alerts", jobs * 2, "ALERT_ONLY", counters={"SEEK": counters}), []))
    monkeypatch.setattr(runner, "send_message", lambda *a: pytest.fail("Unexpected Telegram call"))
    caplog.set_level(logging.INFO)
    assert runner.run(str(config), str(path), "all", dry_run=True) == 0
    assert path.read_bytes() == original
    assert '"already_notified": 2' in caplog.text and '"duplicates": 1' in caplog.text
    for private in ("synthetic-secret", "Food Scientist", "Example Foods", "https://"):
        assert private not in caplog.text and private not in json.dumps(counters)
    assert runner.run(str(config), str(path), "fast") == 0
    saved = State(path)
    assert len(saved.data["records"]) == 1 and saved.was_notified(key)
    assert saved.data["mail_retry"] == ["previous-retry"] and saved.data["mail_seen"] == ["previous-ack"]


def test_role_keyword_in_description_does_not_satisfy_neighbor_card():
    html = card("https://www.seek.com/click/one", "Baker", "Example One") + card("https://www.seek.com/click/two", "Analytical Scientist", "Example Two")
    jobs, ack, c = parse(html, plain("https://nz.seek.com/job/101", "Baker", "Example One"))
    assert not ack and [(j.source_id, j.title) for j in jobs] == [("101", "Baker")]
    assert "Analytical Scientist" not in jobs[0].description
    assert c["unresolved_job_cards"] == 1


def test_corrupt_title_does_not_become_valid_by_concatenating_metadata():
    jobs, ack, c = parse(card("https://nz.seek.com/job/101", "View job"))
    assert not jobs and not ack
    assert c["unresolved_job_cards"] == 1 and c["failure_reasons"]["missing_title"] == 1
