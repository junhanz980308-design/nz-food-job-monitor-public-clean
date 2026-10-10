import json
from unittest.mock import MagicMock

import pytest
from jobwatch import email_alerts as mail


def parse(html, text="", subject="New jobs", platform="SEEK", resolver=None):
    counters = mail.new_mail_counters()
    sender = {"SEEK": "alerts@seek.com", "Indeed": "alerts@indeed.com"}[platform]
    jobs, ack = mail._parse_email(subject, sender, html, text, counters, resolver)
    return jobs, ack, counters[platform]


@pytest.fixture
def network(monkeypatch):
    calls = []
    destinations = {}
    monkeypatch.setattr(mail.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    def head(self, url, **kwargs):
        assert kwargs == dict(allow_redirects=False, timeout=4)
        assert self.trust_env is False
        calls.append(url)
        return MagicMock(status_code=302, headers={"Location": destinations[url]})
    monkeypatch.setattr(mail.requests.Session, "head", head)
    return calls, destinations


def test_canonical_job_card_avoids_all_redundant_tracking_requests():
    href = "https://www.seek.com/click/opaque"
    html = f'<article><h3>Food Scientist</h3><a href="https://nz.seek.com/job/101">View job</a><a href="{href}">Food Scientist</a><a href="{href}#button">Apply now</a><a href="https://vendor.example.test/click/footer">Unsubscribe</a></article>'
    jobs, ack, c = parse(html, f"Food Scientist\n{href}", resolver=mail.RedirectResolver())
    assert ack and len(jobs) == 1 and jobs[0].source_id == "101"
    assert c["redirect_requests"] == 0 and c["canonical_card_reuse"] == 3
    assert c["duplicate_links"] == 2 and not c["failure_reasons"]


def test_one_resolution_per_card_with_distinct_title_button_trackers(network):
    calls, destinations = network
    title, button = "https://www.seek.com/click/title", "https://www.seek.com/click/button"
    destinations[title] = "https://nz.seek.com/job/123"
    html = f'<article><a href="{button}">View job</a><a href="{title}">Food Scientist</a></article>'
    jobs, ack, c = parse(html, resolver=mail.RedirectResolver())
    assert ack and len(jobs) == 1 and calls == [title]
    assert c["canonical_card_reuse"] == 1


def test_navigation_first_in_document_does_not_consume_budget(network):
    calls, destinations = network
    nav = ''.join(f'<a href="https://www.seek.com/click/nav{i}">View all jobs</a>' for i in range(40))
    opaque = ''.join(f'<a href="https://www.seek.com/click/unknown{i}">Details</a>' for i in range(4))
    strong = "https://www.seek.com/click/job"
    destinations[strong] = "https://nz.seek.com/job/111"
    jobs, ack, c = parse(nav + opaque + f'<article><a href="{strong}">Dairy Scientist</a></article>', resolver=mail.RedirectResolver())
    assert len(jobs) == 1 and calls == [strong] and not ack
    assert c["link_decisions"]["navigation"] == 40
    assert c["ambiguous_targets"] == 4 and c["redirect_requests"] == 1
    assert c["failure_reasons"] == {"insufficient_job_evidence": 4}


def test_duplicate_html_plaintext_and_fragment_variants_resolve_once(network):
    calls, destinations = network
    href = "https://nz.indeed.com/pagead/clk?ad=SYNTHETIC"
    destinations[href] = "https://nz.indeed.com/viewjob?jk=abc123"
    html = f'<article><a href="{href}">Food Scientist</a><a href="{href}#button">View job</a></article>'
    jobs, ack, c = parse(html, f"Food Scientist\n{href}", platform="Indeed", resolver=mail.RedirectResolver())
    assert ack and len(jobs) == 1 and calls == [href]
    assert c["unique_link_targets"] == 1 and c["duplicate_links"] == 2


def test_shared_redirect_hop_is_cached_across_messages(network):
    calls, destinations = network
    a, b, shared = [f"https://www.seek.com/click/{s}" for s in ("a", "b", "shared")]
    destinations.update({a: shared, b: shared, shared: "https://nz.seek.com/job/1"})
    resolver = mail.RedirectResolver()
    for url in (a, b):
        jobs, ack, c = parse(f'<article><a href="{url}">Food Scientist</a></article>', resolver=resolver)
        assert ack and len(jobs) == 1
    assert calls == [a, shared, b] and c["redirect_cache_hits"] == 1


def test_later_card_never_inherits_canonical_from_neighbor():
    html = '<div><article><a href="https://nz.seek.com/job/1">Food Scientist</a></article><article><h3>Dairy Technologist</h3><a href="https://vendor.example.test/click/opaque">View job</a></article></div>'
    jobs, ack, c = parse(html, resolver=mail.RedirectResolver())
    assert len(jobs) == 1 and not ack and c["canonical_card_reuse"] == 0
    assert c["failure_reasons"]["reject_external_domain"] == 1


def test_two_canonical_ids_in_one_container_are_not_reused():
    html = '<article><a href="https://nz.seek.com/job/1">Food Scientist</a><a href="https://nz.seek.com/job/2">Dairy Scientist</a><a href="https://vendor.example.test/click/x">View job</a></article>'
    jobs, ack, c = parse(html, resolver=mail.RedirectResolver())
    assert len(jobs) == 2 and not ack and c["canonical_card_reuse"] == 0


def test_table_heading_card_still_extracts_but_neighbor_missing_title_defers(network):
    calls, destinations = network
    href = "https://www.seek.com/click/a"
    destinations[href] = "https://nz.seek.com/job/1"
    html = f'<table class="job-card"><tr><td><strong>Food Scientist</strong></td></tr><tr><td><a href="{href}">View job</a></td></tr></table><article><a href="https://nz.seek.com/job/2">View job</a></article>'
    jobs, ack, c = parse(html, resolver=mail.RedirectResolver())
    assert len(jobs) == 1 and jobs[0].title == "Food Scientist" and not ack
    assert c["failure_reasons"]["missing_title"] == 1


def test_forty_marketing_navigation_targets_are_not_forty_jobs():
    labels = ["Read article", "Privacy policy", "Unsubscribe", "Email preferences"]
    html = ''.join(f'<a href="https://vendor.example.test/click/{i}">{labels[i % 4]}</a>' for i in range(40))
    text = '\n'.join(f'https://vendor.example.test/click/{i}' for i in range(40))
    jobs, ack, c = parse(html, text, subject="Career advice newsletter", platform="Indeed", resolver=mail.RedirectResolver())
    assert ack and not jobs and c["non_job_emails"] == 1
    assert c["redirect_requests"] == 0 and not c["failure_reasons"]
    assert c["unique_link_targets"] == 40 and c["duplicate_links"] == 40


def test_uncertain_newsletter_is_not_acknowledged_for_green_status():
    html = ''.join(f'<a href="https://vendor.example.test/click/{i}">Details</a>' for i in range(40))
    jobs, ack, c = parse(html, subject="Newsletter", platform="Indeed", resolver=mail.RedirectResolver())
    assert not ack and not jobs and c["non_job_emails"] == 0
    assert c["alerts_recognized"] == 0 and c["ambiguous_targets"] == 40
    assert c["redirect_requests"] == 0 and c["failure_reasons"]["insufficient_job_evidence"] == 40


def test_duplicate_ambiguous_targets_count_once_not_per_mime_occurrence():
    href = "https://vendor.example.test/click/synthetic"
    html = ''.join(f'<a href="{href}">Details</a>' for _ in range(40))
    _, ack, c = parse(html, href, subject="Update", platform="Indeed")
    assert not ack and c["unique_link_targets"] == 1 and c["duplicate_links"] == 40
    assert c["failure_reasons"]["insufficient_job_evidence"] == 1


@pytest.mark.parametrize("url,reason", [
    ("http://www.seek.com/click/x", "reject_non_https"),
    ("https://user:synthetic@www.seek.com/click/x", "reject_userinfo"),
    ("https://www.seek.com:444/click/x", "reject_port"),
    ("https://seek.com.evil.test/click/x", "reject_external_domain"),
    ("https://www.indeed.com/clk?ad=synthetic", "reject_cross_platform"),
    ("https://www.seek.com/sign-in", "reject_navigation_path"),
    ("https://www.seek.com/unsupported", "reject_unsupported_path"),
    ("https://www.seek.com:invalid/click/x", "reject_malformed_url"),
])
def test_precise_rejection_reasons_without_requests(url, reason):
    resolver = mail.RedirectResolver()
    assert resolver.resolve(url, "SEEK") == (None, reason)
    assert resolver.requests == 0


def test_budget_not_increased_and_strong_links_before_weaker_card_links(network):
    calls, destinations = network
    a, b = "https://www.seek.com/click/weak", "https://www.seek.com/click/strong"
    destinations[b] = "https://nz.seek.com/job/1"
    resolver = mail.RedirectResolver()
    assert resolver.remaining == 24
    resolver.remaining = 1
    html = f'<article><h3>Dairy Scientist</h3><a href="{a}">Details</a></article><article><a href="{b}">Food Scientist</a></article>'
    jobs, ack, c = parse(html, resolver=resolver)
    assert not ack and len(jobs) == 1 and calls == [b]
    assert c["failure_reasons"]["redirect_request_budget"] == 1


def test_time_budget_has_own_category():
    resolver = mail.RedirectResolver()
    resolver.deadline = 0
    assert resolver.resolve("https://www.seek.com/click/x", "SEEK")[1] == "redirect_time_budget"
    assert resolver.requests == 0


def test_four_hops_allowed_but_no_fifth_request(network):
    calls, destinations = network
    urls = [f"https://www.seek.com/click/{i}" for i in range(6)]
    destinations.update(dict(zip(urls, urls[1:])))
    assert mail.RedirectResolver().resolve(urls[0], "SEEK")[1] == "redirect_hop_limit"
    assert len(calls) == 4


def test_diagnostic_payload_never_includes_inputs():
    private = "SYNTHETIC-SENSITIVE-MARKER"
    html = f'<a href="https://vendor.example.test/click/{private}">Food Scientist</a>'
    _, ack, c = parse(html, resolver=mail.RedirectResolver())
    report = json.dumps(c)
    assert not ack and "reject_external_domain" in report
    assert private not in report and "https://" not in report and "Food Scientist" not in report


def test_retry_record_preserved_for_uncertain_message():
    counters, retry = mail.new_mail_counters(), {"existing-hash"}
    jobs, ack = mail.read_message("Newsletter", "alerts@indeed.com", '<a href="https://vendor.example.test/click/x">Details</a>', "", counters, mail.RedirectResolver(), "existing-hash", retry)
    assert not jobs and not ack and retry == {"existing-hash"}
    assert counters["Indeed"]["emails_deferred"] == 1


def test_anonymous_sibling_divs_do_not_share_job_identity():
    html = '<div><div><a href="https://nz.seek.com/job/1">Food Scientist</a></div><div><a href="https://vendor.example.test/click/other">View job</a></div></div>'
    jobs, ack, c = parse(html, resolver=mail.RedirectResolver())
    assert len(jobs) == 1 and not ack and c["canonical_card_reuse"] == 0


def test_opaque_signature_substring_is_not_mistaken_for_navigation(network):
    calls, destinations = network
    href = "https://www.seek.com/click/synthetic-help-signature"
    destinations[href] = "https://nz.seek.com/job/1"
    jobs, ack, c = parse(f'<a href="{href}">Food Scientist</a>', resolver=mail.RedirectResolver())
    assert ack and len(jobs) == 1 and calls == [href]


def test_contact_links_do_not_create_false_unresolved_jobs():
    html = '<article><a href="https://nz.seek.com/job/1">Food Scientist</a></article><a href="mailto:synthetic@example.test">Email support</a>'
    jobs, ack, c = parse(html, resolver=mail.RedirectResolver())
    assert ack and len(jobs) == 1 and c["redirect_requests"] == 0
