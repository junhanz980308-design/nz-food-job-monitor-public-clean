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
from urllib.parse import parse_qs, unquote, urlparse, urljoin
from html import unescape
import ipaddress
import socket
import time
import requests

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
    url = unescape(href or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    for _ in range(3):
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        target = next((params[k][0] for k in ("url", "target", "redirect", "redirect_url", "dest", "destination", "u", "redirectUrl", "continue", "q") if params.get(k)), "")
        for _ in range(2):
            if target.startswith(("http://", "https://")):
                break
            target = unquote(target)
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
                       parse_failures=0, unknown_coverage=0, coverage="not_checked",
                       non_job_emails=0, emails_acknowledged=0, emails_deferred=0,
                       unsupported_url_patterns=0, failure_reasons={}, url_patterns={})
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
        elif path.rstrip("/") in {"/jobs/view", "/comm/jobs/view", "/jobs/search", "/comm/jobs/search"}:
            ident = parse_qs(parsed.query).get("currentJobId", parse_qs(parsed.query).get("jobId", [""]))[0]
        if re.fullmatch(r"\d+", ident):
            return platform, ident, f"https://www.linkedin.com/jobs/view/{ident}"
    elif platform == "SEEK":
        match = re.fullmatch(r"/job/(\d+)/?", path)
        if match:
            ident = match.group(1)
    elif platform == "Indeed":
        ident = parse_qs(parsed.query).get("jk", [""])[0]
        if not re.fullmatch(r"[\w-]+", ident) or path.rstrip("/") not in {"/viewjob", "/m/viewjob", "/rc/clk", "/pagead/clk", "/clk", "/company/clk"}:
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
    if platform == "Indeed":
        return platform, ident, f"https://nz.indeed.com/viewjob?jk={ident}"
    clean = canonical_url(url)
    if platform in {"Trade Me", "Adzuna"}:
        clean = clean.split("?", 1)[0]
    return platform, ident, clean


TRACK_PATH = re.compile(r"^/(?:ls/click|ss/c|uni/ss/c|click|clicks|track|tracking|redirect|go|rc/clk|pagead/clk|clk)(?:/|$)", re.I)
NON_JOB = re.compile(r"unsubscribe|preferences|privacy|help|settings|login|sign.?in|manage.alert|view.all|see.all|search.jobs|browse.jobs", re.I)


def bump(counter, field, label):
    values = counter[field]
    values[label] = values.get(label, 0) + 1


def url_pattern(href):
    try:
        parsed = urlparse(unwrap_target(href))
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.port not in (None, 443):
            return "unsafe_url"
        if identify_job_url(href):
            return "direct_or_embedded_job"
        if TRACK_PATH.match(parsed.path):
            return "opaque_first_party_tracker" if trusted_host(parsed.hostname or "") else "opaque_external_tracker"
        if NON_JOB.search(parsed.path):
            return "navigation"
        return "unsupported_first_party" if trusted_host(parsed.hostname or "") else "external_link"
    except (ValueError, TypeError):
        return "unsafe_url"


class RedirectResolver:
    """HEAD-only, first-party tracker resolution; never request a job page/body.

    External email vendors, HTML/JS redirects, login flows and GET-only endpoints
    remain unsupported. A scan-wide request/time budget prevents email amplification.
    """
    def __init__(self):
        self.cache = {}
        self.remaining = 24
        self.deadline = time.monotonic() + 45

    def resolve(self, href, platform):
        key = (href, platform)
        if key in self.cache:
            return self.cache[key]
        current = unwrap_target(href)
        visited = set()
        result, reason = None, "redirect_limit"
        for _ in range(4):
            if urlparse(current).scheme != "https":
                reason = "redirect_not_allowed"
                break
            job = identify_job_url(current)
            if job:
                result, reason = (job, "resolved") if job[0] == platform else (None, "platform_mismatch")
                break
            try:
                parsed = urlparse(current)
                if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443)
                        or platform_for_host(parsed.hostname or "") != platform or not TRACK_PATH.match(parsed.path)):
                    reason = "redirect_not_allowed"
                    break
                if current in visited:
                    reason = "redirect_loop"
                    break
                if self.remaining <= 0 or time.monotonic() >= self.deadline:
                    reason = "redirect_budget"
                    break
                visited.add(current)
                self.remaining -= 1
                addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                    reason = "redirect_not_allowed"
                    break
                # No browser cookies, proxy environment, authorization or Gmail headers.
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.head(current, allow_redirects=False, timeout=4)
                    try:
                        location = response.headers.get("Location", "")
                        if response.status_code not in {301, 302, 303, 307, 308} or not location:
                            reason = "redirect_not_http"
                            break
                        current = urljoin(current, location)
                    finally:
                        response.close()
            except Exception:
                reason = "redirect_request_failed"
                break
        self.cache[key] = (result, reason)
        return self.cache[key]


def prepare_links(html, text, platform, resolver, counter):
    soup = _soup(html)
    replacements = {}
    missing = False
    subject_links = []
    for a in soup.find_all("a", href=True):
        subject_links.append((a.get("href", ""), a.get_text(" ", strip=True), a))
    for raw in re.findall(r"https?://[^\s<>]+", text or ""):
        subject_links.append((raw.rstrip(".,;)>"), "", None))
    for href, label, anchor in subject_links:
        pattern = url_pattern(href)
        bump(counter, "url_patterns", pattern)
        if pattern == "direct_or_embedded_job":
            continue
        # Do not follow unsubscribe, account, bulk search or marketing navigation.
        navigation = bool(NON_JOB.search(label) or re.search(r"unsubscribe|preferences|privacy", href, re.I))
        suspicious = not navigation and ("tracker" in pattern or re.search(r"/jobs?/", href, re.I) or re.search(r"\b(?:view job|apply|scientist|technologist|fellow|researcher)\b", label, re.I))
        if not suspicious:
            continue
        result, reason = resolver.resolve(href, platform) if resolver else (None, "redirect_unresolved")
        if result:
            if anchor is not None:
                anchor["href"] = result[2]
            replacements[href] = result[2]
        else:
            missing = True
            counter["unsupported_url_patterns"] += 1
            bump(counter, "failure_reasons", reason)
    for raw, clean in replacements.items():
        text = text.replace(raw, clean)
    return str(soup), text, missing


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
        if any(a is not anchor and not identify_job_url(a.get("href", ""))
               and "tracker" in url_pattern(a.get("href", ""))
               and not NON_JOB.search(a.get_text(" ", strip=True))
               for a in parent.find_all("a", href=True)):
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
        value = anchor.get("aria-label", "") or anchor.get("title", "")
        if not value and anchor.find("img"):
            value = anchor.find("img").get("alt", "")
    if not _valid_title(value):
        card = _card(anchor)
        choices = card.find_all(["h1", "h2", "h3", "h4", "a"])
        # Titles in table-based email cards commonly use styled spans/strong text.
        choices += card.select("[class*=title], [class*=Title], strong, b")
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
    address = parseaddr(sender)[1]
    allowed = "@" in address and trusted_host(address.rsplit("@", 1)[1].lower())
    # "Recommended reading" / "career advice" are not job alerts.
    return allowed and bool(re.search(r"\b(?:jobs?|job alerts?|new roles|new vacancies|recommended jobs|job matches)\b", subject, re.I)) and not bool(re.search(r"career advice|job.search tips|webinar|newsletter", subject, re.I))


def _parse_email(subject, sender, html, text, counters, resolver=None):
    address = parseaddr(sender)[1]
    platform = platform_for_host(address.rsplit("@", 1)[1].lower()) if "@" in address else "Unknown"
    counter = counters[platform]
    counter["emails_checked"] += 1
    if platform == "Unknown":
        counter["unknown_coverage"] += 1
        counter["non_job_emails"] += 1
        return [], True
    recognized = _is_job_alert(subject, sender)
    counter["alerts_recognized"] += int(recognized)
    try:
        # Do not issue tracking requests merely because a sender is a job board.
        evidence = recognized or bool(re.search(r"view job|job alert|scientist|technologist|research fellow|postdoctoral", _soup(html).get_text(" ", strip=True) + " " + text, re.I))
        html, text, unresolved = prepare_links(html, text, platform, resolver if evidence else None, counter)
        html_jobs = extract_jobs_from_html(html)
        text_jobs = extract_jobs_from_text(text)
        by_id = {(job.source, job.source_id): job for job in text_jobs + html_jobs}
        jobs = [j for j in by_id.values() if j.source == f"{platform} email alert"]
        hrefs = [a.get("href", "") for a in _soup(html).find_all("a", href=True)] + re.findall(r"https?://[^\s<>]+", text)
        supported = {r[1] for h in hrefs if (r := identify_job_url(h)) and r[0] == platform}
        missing_title = supported - {j.source_id for j in jobs}
        # Unsupported trackers alone do not prove marketing is a job alert.
        recognized = recognized or bool(supported) or bool(re.search(r"\b(?:view job|apply now|job alert)\b", _soup(html).get_text(" ", strip=True) + " " + text, re.I))
        if recognized and not _is_job_alert(subject, sender):
            counter["alerts_recognized"] += 1
        counter["links_extracted"] += len(jobs)
        clear_non_job = bool(re.search(r"newsletter|recommended reading|career advice|job.search tips|security alert|password|invitation|connection request", subject, re.I))
        uncertain = not recognized and unresolved and not clear_non_job
        incomplete = (recognized and (not jobs or unresolved or bool(missing_title))) or uncertain
        if uncertain:
            bump(counter, "failure_reasons", "uncertain_message")
        if incomplete:
            counter["parse_failures"] += 1
            counter["unknown_coverage"] += 1
            if not jobs:
                bump(counter, "failure_reasons", "no_extractable_jobs")
            if missing_title:
                bump(counter, "failure_reasons", "missing_title")
        elif not recognized:
            counter["non_job_emails"] += 1
            counter["unknown_coverage"] += 1
        return jobs, not incomplete
    except Exception:
        counter["parse_failures"] += 1
        counter["unknown_coverage"] += 1
        bump(counter, "failure_reasons", "parser_exception")
        raise SourceError("Email parsing failed") from None


def _email_to_jobs(subject, sender, html, text, counters=None):
    return _parse_email(subject, sender, html, text, counters or new_mail_counters())[0]


def read_message(subject, sender, html, text, counters, resolver, hashed, retry):
    address = parseaddr(sender)[1]
    platform = platform_for_host(address.rsplit("@", 1)[1].lower()) if "@" in address else "Unknown"
    try:
        jobs, acknowledge = _parse_email(subject, sender, html, text, counters, resolver)
    except SourceError:
        jobs, acknowledge = [], False
    counters[platform]["emails_acknowledged" if acknowledge else "emails_deferred"] += 1
    if acknowledge:
        retry.discard(hashed)
    else:
        retry.add(hashed)
    return jobs, acknowledge


def _gmail_api_messages(seen: set[str], after: datetime | None = None, counters: dict | None = None, retry: set | None = None, before: datetime | None = None) -> tuple[list[Job], list[str], int]:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    token = json.loads(os.environ["GMAIL_OAUTH_TOKEN_JSON"])
    credentials = Credentials.from_authorized_user_info(token, GMAIL_SCOPE)
    if not credentials.refresh_token:
        raise SourceError("Gmail OAuth credentials have no refresh token; re-authorize offline access")
    service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
    counters = counters if counters is not None else new_mail_counters()
    retry = retry if retry is not None else set()
    resolver = RedirectResolver()
    jobs: list[Job] = []
    new_ids: list[str] = []
    examined = 0
    next_page = None
    while True:
        domains = ["linkedin.com", "seek.com", "seek.co.nz", "seek.com.au", "indeed.com", "trademe.co.nz", "adzuna.co.nz"]
        since = f"after:{int(after.timestamp())}" if after else "newer_than:4d"
        if before:
            since += f" before:{int(before.timestamp())}"
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
            extracted, ack = read_message(subject, sender, html, txt, counters, resolver, hashed, retry)
            jobs.extend(extracted)
            if ack:
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


def _imap_messages(seen: set[str], after: datetime | None = None, counters: dict | None = None, retry: set | None = None, before: datetime | None = None) -> tuple[list[Job], list[str], int]:
    username, password = os.environ["GMAIL_IMAP_USER"], os.environ["GMAIL_IMAP_APP_PASSWORD"]
    counters = counters if counters is not None else new_mail_counters()
    retry = retry if retry is not None else set()
    resolver = RedirectResolver()
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
            terms = ["SINCE", start, "FROM", f'"{sender_domain}"']
            if before:
                terms += ["BEFORE", before.strftime("%d-%b-%Y")]
            typ, data = connection.uid("search", None, *terms)
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
            extracted, ack = read_message(subject, sender, "\n".join(html_parts), "\n".join(text_parts), counters, resolver, hashed, retry)
            jobs.extend(extracted)
            if ack:
                new_ids.append(hashed)
            examined += 1
    return jobs, new_ids, examined


def gmail_alerts(state: dict) -> tuple[SourceResult, list[str]]:
    sid = "gmail-alerts"
    retry = set(state.get("mail_retry", []))
    seen = set(state.get("mail_seen", [])) - retry
    before = datetime.fromisoformat(state["mail_scan_before"]) if state.get("mail_scan_before") else None
    after = datetime.fromisoformat(state["mail_scan_after"]) if state.get("mail_scan_after") else None
    counters = new_mail_counters()
    try:
        if os.getenv("GMAIL_OAUTH_TOKEN_JSON"):
            jobs, new_ids, count = _gmail_api_messages(seen, after, counters, retry, before)
            method = "Gmail OAuth API"
        elif os.getenv("GMAIL_IMAP_USER") and os.getenv("GMAIL_IMAP_APP_PASSWORD"):
            jobs, new_ids, count = _imap_messages(seen, after, counters, retry, before)
            method = "Gmail IMAP app password"
        else:
            return SourceResult(sid, [], "PENDING", "Configure OAuth or IMAP App Password", counters), []
    except Exception:
        for counter in counters.values():
            counter["coverage"] = "scan_failed"
        raise MailScanError(counters) from None
    state["mail_retry"] = sorted(retry)
    for counter in counters.values():
        counter["coverage"] = ("no_emails_checked" if not counter["emails_checked"] else
                               "unknown_or_partial" if counter["unknown_coverage"] else "parsed")
    partial = bool(retry) or any(c["parse_failures"] for c in counters.values())
    return SourceResult(sid, jobs, "PARTIAL" if partial else "ALERT_ONLY", f"{method}: {count} messages checked; {len(jobs)} job links extracted", counters), new_ids
