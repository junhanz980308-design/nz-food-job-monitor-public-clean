"""Read *official alert emails*, not private SEEK/LinkedIn recruitment APIs."""
from __future__ import annotations

import base64
import binascii
import email
import email.policy
import imaplib
import json
import logging
import os
import re
import warnings
from datetime import datetime, timedelta, timezone
from email.message import Message
from email.utils import parseaddr
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning

from .models import Job, SourceResult, canonical_url, digest
from .sources import SourceError

LOG = logging.getLogger(__name__)
GMAIL_SCOPE = ["https://www.googleapis.com/auth/gmail.readonly"]
TRUSTED = ("linkedin.com", "seek.com", "seek.co.nz", "seek.com.au", "indeed.com", "trademe.co.nz", "adzuna.co.nz")
CTA = re.compile(r"^(view|see|apply|find out|details|learn more|read more|click|open|new jobs|job alert)\b", re.I)


def _soup(html: str):
    # BeautifulSoup's locator warning can quote the input; email data stays private.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", MarkupResemblesLocatorWarning)
        return BeautifulSoup(html or "", "html.parser")


def trusted_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in TRUSTED)


def unwrap_target(href: str) -> str:
    url = href or ""
    for _ in range(3):
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        target = next((params[k][0] for k in ("url", "target", "redirect", "redirect_url", "dest", "destination", "u") if params.get(k)), "")
        if target.startswith(("http://", "https://")):
            url = target
            continue
        # Some email trackers encode target URLs in a path or query.
        decoded = unquote(url)
        match = re.search(r"https?://(?:[\w-]+\.)*(?:linkedin\.com|seek\.com|seek\.co\.nz|seek\.com\.au|indeed\.com|trademe\.co\.nz|adzuna\.co\.nz)/[^\s\"<>]*", decoded, re.I)
        if match and not trusted_host(parsed.hostname or ""):
            url = match.group(0)
        break
    return url


PLATFORM_DOMAINS = {
    "LinkedIn": ("linkedin.com",), "SEEK": ("seek.com", "seek.co.nz", "seek.com.au"),
    "Indeed": ("indeed.com",), "Trade Me": ("trademe.co.nz",), "Adzuna": ("adzuna.co.nz",),
}


def platform_for_host(host: str) -> str:
    return next((name for name, domains in PLATFORM_DOMAINS.items()
                 if any(host == d or host.endswith("." + d) for d in domains)), "Unknown")


def new_mail_counters() -> dict:
    return {name: dict(emails_checked=0, alerts_recognized=0, links_extracted=0,
                       parse_failures=0, unknown_coverage=0, coverage="not_checked")
            for name in (*PLATFORM_DOMAINS, "Unknown")}


class MailScanError(SourceError):
    def __init__(self, counters):
        super().__init__("Email scan failed; platform coverage incomplete")
        self.counters = counters


def identify_job_url(href: str) -> tuple[str, str, str] | None:
    try:
        url = unwrap_target(href)
        parsed = urlparse(url)
        if not canonical_url(url):
            return None
    except (ValueError, TypeError):
        return None
    platform = platform_for_host((parsed.hostname or "").lower())
    path, ident = parsed.path, ""
    if platform == "LinkedIn":
        match = re.fullmatch(r"/(?:comm/)?jobs/view/(?:[^/]*-)?(\d+)/?", path)
        if match:
            ident = match.group(1)
            return platform, ident, f"https://www.linkedin.com/jobs/view/{ident}"
    elif platform == "SEEK":
        match = re.fullmatch(r"/job/(\d+)/?", path)
        if match:
            ident = match.group(1)
    elif platform == "Indeed":
        ident = parse_qs(parsed.query).get("jk", [""])[0]
        if not re.fullmatch(r"[\w-]+", ident) or path.rstrip("/") not in {"/viewjob", "/m/viewjob", "/rc/clk", "/pagead/clk"}:
            ident = ""
    elif platform == "Trade Me" and "/jobs/" in path:
        match = re.search(r"/(?:listing/(\d+)/?|auction-(\d+)\.htm)$", path)
        if match:
            ident = match.group(1) or match.group(2)
    elif platform == "Adzuna":
        match = re.fullmatch(r"/details/(\d+)/?", path)
        if match:
            ident = match.group(1)
    if not ident:
        return None
    clean = canonical_url(url)
    if platform in {"Trade Me", "Adzuna"}:
        clean = clean.split("?", 1)[0]
    return platform, ident, clean


def _card(anchor):
    """Never borrow a heading or food context from another job in a digest."""
    chosen = anchor
    own = identify_job_url(anchor.get("href", ""))
    for parent in list(anchor.parents)[:6]:
        if parent.name in {"body", "html", "[document]"}:
            break
        ids = {item[:2] for a in parent.find_all("a", href=True)
               if (item := identify_job_url(a.get("href", "")))}
        if ids - {own[:2]}:
            break
        if len(parent.get_text(" ", strip=True)) > 900:
            break
        chosen = parent
    return chosen


def _valid_title(value: str) -> bool:
    return 7 <= len(value) <= 130 and not CTA.match(value) and not re.search(r"https?://|\S+@\S+", value)


def _best_title(anchor) -> tuple[str, str]:
    value = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
    if not _valid_title(value):
        card = _card(anchor)
        choices = card.find_all(["h1", "h2", "h3", "h4", "a"])
        value = next((v for node in choices if _valid_title(v := node.get_text(" ", strip=True))), "")
    company = ""
    pattern = re.match(r"^(.*?)\s+at\s+(.{3,100})$", value, re.I)
    if pattern:
        value, company = pattern.groups()
    if not company:
        # Preserve explicit employer lines, without greedily consuming the whole
        # card (including location, another title, or description).
        for line in _card(anchor).stripped_strings:
            employer = re.fullmatch(r"at ([^@<>]{3,100})", line.strip())
            if employer and not re.search(r"https?://", line):
                company = employer.group(1)
                break
    return value, company


def extract_jobs_from_html(html: str) -> list[Job]:
    soup = _soup(html)
    jobs: dict[str, Job] = {}
    for anchor in soup.find_all("a", href=True):
        result = identify_job_url(anchor.get("href", ""))
        if not result:
            continue
        platform, ident, url = result
        title, company = _best_title(anchor)
        if not _valid_title(title):
            continue
        card = _card(anchor).get_text(" ", strip=True)
        candidate = Job(source=f"{platform} email alert", source_id=ident, title=title,
                        company=company, description=card[:500], url=url)
        # A CTA following the title link must not erase a better extraction.
        if url not in jobs or len(candidate.description) > len(jobs[url].description):
            jobs[url] = candidate
    return list(jobs.values())


def extract_jobs_from_text(text: str) -> list[Job]:
    jobs: dict[str, Job] = {}
    lines = [s.strip() for s in (text or "").splitlines() if s.strip()]
    for index, line in enumerate(lines):
        match = re.search(r"https?://\S+", line)
        if not match:
            continue
        result = identify_job_url(match.group(0).rstrip(".,;)>"))
        if not result:
            continue
        platform, ident, url = result
        candidate = line[:match.start()].strip(" :-–") or (lines[index - 1] if index else "")
        if _valid_title(candidate):
            jobs[url] = Job(f"{platform} email alert", ident, candidate, description=candidate, url=url)
    return list(jobs.values())


def _extract_gmail_parts(payload: dict) -> tuple[str, str]:
    html_parts, text_parts = [], []

    def visit(part: dict):
        if part.get("filename"):
            return
        mime = part.get("mimeType", "")
        raw = (part.get("body") or {}).get("data")
        if raw:
            try:
                decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", errors="replace")
                if mime == "text/html":
                    html_parts.append(decoded)
                elif mime == "text/plain":
                    text_parts.append(decoded)
            except (ValueError, binascii.Error):
                pass
        for child in part.get("parts", []) or []:
            visit(child)

    visit(payload)
    return "\n".join(html_parts), "\n".join(text_parts)


def _is_job_alert(subject: str, sender: str) -> bool:
    low = subject.lower()
    address = parseaddr(sender)[1]
    allowed = "@" in address and trusted_host(address.rsplit("@", 1)[1].lower())
    return allowed and bool(re.search(r"\b(job|jobs|career|vacanc|opportunit|role|recommended|match)\w*\b", low))


def _email_to_jobs(subject: str, sender: str, html: str, text: str, counters: dict | None = None) -> list[Job]:
    address = parseaddr(sender)[1]
    platform = platform_for_host(address.rsplit("@", 1)[1].lower()) if "@" in address else "Unknown"
    counter = counters[platform] if counters is not None else None
    if counter is not None:
        counter["emails_checked"] += 1
    if platform == "Unknown":
        if counter is not None:
            counter["unknown_coverage"] += 1
        return []
    subject_alert = _is_job_alert(subject, sender)
    if counter is not None:
        counter["alerts_recognized"] += int(subject_alert)
    # Strict sender check remains. Merge both MIME alternatives: a partially
    # parsed HTML digest must not hide a valid plaintext job.
    try:
        html_jobs = extract_jobs_from_html(html)
        text_jobs = extract_jobs_from_text(text)
        by_id = {(job.source, job.source_id): job for job in text_jobs + html_jobs}
        jobs = [job for job in by_id.values() if job.source == f"{platform} email alert"]
        recognized = subject_alert or bool(jobs)
        if counter is not None:
            counter["alerts_recognized"] += int(bool(jobs) and not subject_alert)
            counter["links_extracted"] += len(jobs)
            counter["parse_failures"] += int(recognized and not jobs)
            # Even a successful parse doesn't prove complete template coverage.
            hrefs = [a.get("href", "") for a in _soup(html).find_all("a", href=True)]
            unsupported = any(not identify_job_url(h) and re.search(r"job|click|redirect", h, re.I) for h in hrefs)
            supported_ids = {r[:2] for h in hrefs if (r := identify_job_url(h))}
            missing_titles = len(supported_ids) > len(jobs)
            counter["unknown_coverage"] += int(not jobs or unsupported or missing_titles)
        return jobs if recognized else []
    except Exception:
        # Never emit an exception containing an email fragment or private URL.
        if counter is not None:
            counter["parse_failures"] += 1
            counter["unknown_coverage"] += 1
        raise SourceError("Email parsing failed") from None


def _gmail_api_messages(seen: set[str], after: datetime | None = None, counters: dict | None = None) -> tuple[list[Job], list[str], int]:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token = json.loads(os.environ["GMAIL_OAUTH_TOKEN_JSON"])
    credentials = Credentials.from_authorized_user_info(token, GMAIL_SCOPE)
    if not credentials.refresh_token:
        raise SourceError("Gmail OAuth credentials have no refresh token; re-authorize offline access")
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    jobs: list[Job] = []
    new_ids: list[str] = []
    examined = 0
    next_page = None
    while True:
        domains = ["linkedin.com", "seek.com", "seek.co.nz", "seek.com.au", "indeed.com", "trademe.co.nz", "adzuna.co.nz"]
        since = f"after:{int(after.timestamp())}" if after else "newer_than:4d"
        query = since + " {" + " ".join("from:" + d for d in domains) + "}"
        payload = service.users().messages().list(
            userId="me", q=query, maxResults=100, pageToken=next_page,
        ).execute()
        for row in payload.get("messages", []):
            message_id = row["id"]
            hashed = digest("gmail:" + message_id)
            if hashed in seen:
                continue
            # Do not commit an ID until the message was successfully read.
            msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender, subject = headers.get("from", ""), headers.get("subject", "")
            html, txt = _extract_gmail_parts(msg.get("payload", {}))
            jobs.extend(_email_to_jobs(subject, sender, html, txt, counters))
            new_ids.append(hashed)
            examined += 1
            if examined > 500:
                raise SourceError("Mail scan limit exceeded; narrow the dedicated alert mailbox before initializing")
        next_page = payload.get("nextPageToken")
        if next_page and examined >= 500:
            raise SourceError("Mail scan limit exceeded; narrow the dedicated alert mailbox before initializing")
        if not next_page:
            break
    return jobs, new_ids, examined


def _imap_messages(seen: set[str], after: datetime | None = None, counters: dict | None = None) -> tuple[list[Job], list[str], int]:
    username, password = os.environ["GMAIL_IMAP_USER"], os.environ["GMAIL_IMAP_APP_PASSWORD"]
    jobs: list[Job] = []
    new_ids: list[str] = []
    examined = 0
    with imaplib.IMAP4_SSL("imap.gmail.com", 993) as connection:
        connection.login(username, password)
        typ, _ = connection.select("INBOX", readonly=True)
        if typ != "OK":
            raise SourceError("IMAP mailbox selection failed")
        _, validity = connection.response("UIDVALIDITY")
        if not validity or not validity[0]:
            raise SourceError("IMAP UIDVALIDITY missing")
        epoch = validity[0].decode("ascii")
        start = (after or datetime.now(timezone.utc) - timedelta(days=4)).strftime("%d-%b-%Y")
        uid_set: set[bytes] = set()
        for sender_domain in TRUSTED:
            typ, data = connection.uid("search", None, "SINCE", start, "FROM", f'"{sender_domain}"')
            if typ != "OK":
                raise SourceError("IMAP search failed")
            uid_set.update((data[0] or b"").split())
        uid_list = sorted(uid_set, key=lambda x: int(x))
        if sum(digest(f"imap:{username}:{epoch}:{uid.decode('ascii')}") not in seen for uid in uid_list) > 500:
            raise SourceError("Mail scan limit exceeded; narrow the dedicated alert mailbox before initializing")
        for uid in uid_list:
            raw_id = uid.decode("ascii", errors="ignore")
            hashed = digest(f"imap:{username}:{epoch}:{raw_id}")
            if hashed in seen:
                continue
            typ, payload = connection.uid("fetch", uid, "(RFC822)")
            if typ != "OK":
                raise SourceError("IMAP message fetch failed")
            raw = next((part[1] for part in payload if isinstance(part, tuple)), None)
            if not raw:
                raise SourceError("IMAP message body missing")
            msg: Message = email.message_from_bytes(raw, policy=email.policy.default)
            subject, sender = str(msg.get("Subject", "")), str(msg.get("From", ""))
            html_parts, text_parts = [], []
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    continue
                if part.get_content_type() not in ("text/plain", "text/html"):
                    continue
                body = part.get_content()
                (html_parts if part.get_content_type() == "text/html" else text_parts).append(body)
            jobs.extend(_email_to_jobs(subject, sender, "\n".join(html_parts), "\n".join(text_parts), counters))
            new_ids.append(hashed)
            examined += 1
    return jobs, new_ids, examined


def gmail_alerts(state: dict) -> tuple[SourceResult, list[str]]:
    sid = "gmail-alerts"
    seen = set(state.get("mail_seen", []))
    after = datetime.fromisoformat(state["mail_scan_after"]) if state.get("mail_scan_after") else None
    counters = new_mail_counters()
    try:
        if os.getenv("GMAIL_OAUTH_TOKEN_JSON"):
            jobs, new_ids, count = _gmail_api_messages(seen, after, counters)
            method = "Gmail OAuth API"
        elif os.getenv("GMAIL_IMAP_USER") and os.getenv("GMAIL_IMAP_APP_PASSWORD"):
            jobs, new_ids, count = _imap_messages(seen, after, counters)
            method = "Gmail IMAP app password"
        else:
            return SourceResult(sid, [], "PENDING", "Configure OAuth or IMAP App Password", counters), []
    except Exception:
        for counter in counters.values():
            counter["coverage"] = "scan_failed"
        raise MailScanError(counters) from None
    for counter in counters.values():
        counter["coverage"] = ("no_emails_checked" if not counter["emails_checked"] else
                               "unknown_or_partial" if counter["unknown_coverage"] else "parsed")
    return SourceResult(sid, jobs, "ALERT_ONLY", f"{method}: {count} messages checked; {len(jobs)} job links extracted", counters), new_ids
