"""Hand-written examples only; never load real mailbox samples."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jobwatch import email_alerts as mail

TEMPLATE = (Path(__file__).parent / "fixtures/seek_linked_table.html").read_text()


def parse(html, text="", resolver=None):
    counters = mail.new_mail_counters()
    jobs, ack = mail.read_message("New jobs", "alerts@seek.com", html, text, counters, resolver, "synthetic-message", set())
    return jobs, ack, counters["SEEK"]


@pytest.mark.parametrize("title", [
    "Food Quality Officer", "Quality Assurance Officer", "QA Technician", "QC Analyst",
    "Food Safety Specialist", "Quality Technologist", "Food Quality Manager", "HACCP Coordinator",
    "Food Compliance Officer", "Regulatory Affairs Officer", "Quality Auditor", "Food Inspector",
    "Food Safety Auditor", "Quality Inspector", "Safety Officer", "Senior Food Quality Officer",
])
def test_complete_quality_title_resolves_only_to_a_verified_canonical_id(title, monkeypatch):
    tracker = "https://www.seek.com/click/synthetic-quality"
    canonical = "https://nz.seek.com/job/123"
    calls = []
    monkeypatch.setattr(mail.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 443))])
    def head(self, url, **kwargs):
        calls.append(url)
        return MagicMock(status_code=302, headers={"Location": canonical})
    monkeypatch.setattr(mail.requests.Session, "head", head)
    html = f'<a href="{tracker}">{title}</a>'
    jobs, ack, c = parse(html)
    assert not jobs and not ack and c["failure_reasons"] == {"resolution_unavailable": 1, "no_extractable_jobs": 1}
    jobs, ack, c = parse(html, resolver=mail.RedirectResolver())
    assert ack and [(j.source_id, j.title, j.url) for j in jobs] == [("123", title, canonical)]
    assert calls == [tracker] and c["redirect_requests"] == 1
    text_jobs = mail.extract_jobs_from_text(f"Your saved search\n{title}\n{canonical}")
    assert [(j.source_id, j.title) for j in text_jobs] == [("123", title)]


def test_quality_proximity_preserves_eighteen_jobs_and_sixteen_ambiguous_targets():
    # This models the reported counts, not the identities/content of live emails.
    titles = ["Food Scientist", "NPD Technologist", "Research Fellow",
              "Food Quality Officer", "Regulatory Affairs Officer", "Food Inspector"] * 3
    html, text, expected = "", "", {}
    for i, title in enumerate(titles, 1):
        company = f"Example Employer {i}"
        html += TEMPLATE.format(href=f"https://www.seek.com/click/synthetic-card-{i}",
                                title=title, company=company, location="Example City", salary="Example salary")
        text += f"{title}\n{company}\n\nExample City\nExample salary\n\n[https://nz.seek.com/job/{i}]\n"
        expected[str(i)] = (title, company)
    for i in range(16):
        html += ('<article><p>food quality QA QC safety officer auditor inspector regulatory compliance</p>'
                 f'<a href="https://www.seek.com/click/synthetic-unknown-{i}">Details</a></article>')
    jobs, ack, c = parse(html, text, mail.RedirectResolver())
    assert {j.source_id: (j.title, j.company) for j in jobs} == expected
    assert not ack and c["emails_deferred"] == 1
    assert c["structured_job_cards"] == c["canonical_card_reuse"] == c["mime_card_reuse"] == 18
    assert c["unresolved_job_cards"] == c["redirect_requests"] == 0
    assert c["ambiguous_targets"] == 16
    assert c["failure_reasons"] == {"insufficient_job_evidence": 16}
    for private in ("Example Employer", "synthetic-unknown", "https://", "Food Scientist"):
        assert private not in json.dumps(c)


@pytest.mark.parametrize("label", ["Quality", "Quality Assurance", "Quality Control", "QA", "QC", "Safety", "Officer", "Regulatory", "Regulatory Affairs", "Compliance", "Details"])
def test_incomplete_labels_do_not_turn_opaque_links_into_jobs(label):
    jobs, ack, c = parse(f'<a href="https://www.seek.com/click/synthetic">{label}</a>', resolver=mail.RedirectResolver())
    assert not jobs and not ack
    assert c["ambiguous_targets"] == 1 and c["redirect_requests"] == 0
    assert c["failure_reasons"] == {"insufficient_job_evidence": 1, "no_extractable_jobs": 1}


def test_new_quality_title_boundary_does_not_borrow_neighbor_employer_or_identity():
    html = '''<div><div><a href="https://nz.seek.com/job/101">Food Quality Officer</a><p>at Example One</p></div>
    <div><a href="https://www.seek.com/click/synthetic">Food Compliance Officer</a><p>at Example Two</p></div></div>'''
    jobs, ack, c = parse(html)
    assert not ack and [(j.source_id, j.title, j.company) for j in jobs] == [("101", "Food Quality Officer", "Example One")]
    assert "Example Two" not in jobs[0].description
    assert c["canonical_card_reuse"] == 0


@pytest.mark.parametrize("target,reason", [
    ("http://www.seek.com/click/synthetic", "reject_non_https"),
    ("https://vendor.example.test/click/synthetic", "reject_external_domain"),
    ("https://www.seek.com:444/click/synthetic", "reject_port"),
])
def test_recognized_quality_title_does_not_bypass_url_security(target, reason):
    jobs, ack, c = parse(f'<a href="{target}">Food Quality Officer</a>', resolver=mail.RedirectResolver())
    assert not jobs
    assert not ack and c["failure_reasons"] == {reason: 1, "no_extractable_jobs": 1}
    assert c["redirect_requests"] == 0
