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
                       unsupported_url_patterns=0, failure_reasons={}, url_patterns={},
                       link_decisions={}, link_occurrences=0, unique_link_targets=0,
                       duplicate_links=0, canonical_card_reuse=0, structured_job_cards=0, unresolved_job_cards=0,
                       mime_card_reuse=0, redirect_requests=0,
                       redirect_cache_hits=0, unresolved_job_targets=0, ambiguous_targets=0)
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


NAV_PATH = re.compile(r"^/(?:unsubscribe|preferences|privacy|help|settings|login|sign-in|signin|manage-alerts|jobs/search)(?:/|$)", re.I)


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
        if NAV_PATH.search(parsed.path):
            return "navigation"
        return "unsupported_first_party" if trusted_host(parsed.hostname or "") else "external_link"
    except (ValueError, TypeError):
        return "unsafe_url"


# Only explicit navigation/marketing evidence is ignored. "Details" is ambiguous.
NAV_LABEL = re.compile(r"^(?:unsubscribe|manage (?:job )?alerts?|email preferences|preferences|privacy(?: policy)?|terms(?: of use)?|help(?: centre| center)?|settings|sign in|log in|view (?:all|more) jobs|see (?:all|more) jobs|browse jobs|search jobs|save (?:this )?job|share(?: this job)?|company profile|view company|view in browser|contact us|download (?:the )?app|follow us)$", re.I)
MARKETING_LABEL = re.compile(r"^(?:read (?:more|article)|read our newsletter|career advice|job search tips|register for (?:the )?webinar|upgrade(?: to premium)?|learn about premium|recommended reading)$", re.I)
JOB_CTA = re.compile(r"^(?:view (?:this )?job|view (?:job )?details|apply(?: now| for (?:this )?job)?|job details)$", re.I)
JOB_ROLE = re.compile(r"\b(?:scientist|technologist|researcher|fellow|postdoc(?:toral)?|engineer|analyst|technician|developer|manager|coordinator|specialist)\b", re.I)
CARD_CLASS = re.compile(r"(?:^|[ _-])job[ _-]?(?:card|item|row|result)(?:$|[ _-])", re.I)
TITLE_SELECTOR = "h1, h2, h3, h4, [class*=title], [class*=Title], strong, b"


def link_key(href):
    # Drop fragments only: tracking query values can be signed and must survive.
    try:
        return urlparse(unwrap_target(href))._replace(fragment="").geturl()
    except (ValueError, TypeError):
        return href or ""


def anchor_label(anchor):
    return re.sub(r"\s+", " ", anchor.get_text(" ", strip=True) or anchor.get("aria-label", "")
                  or anchor.get("title", "") or (anchor.find("img").get("alt", "") if anchor.find("img") else "")).strip()


def explicit_card(node):
    return node.name in {"article", "li"} or node.has_attr("data-job-id") or bool(CARD_CLASS.search(" ".join(node.get("class", []))))


def title_value(value):
    return _valid_title(value) and not NAV_LABEL.fullmatch(value) and not MARKETING_LABEL.fullmatch(value)


def _style(node, property_name):
    return next((value.strip().lower() for declaration in node.get("style", "").split(";")
                 if ":" in declaration for key, value in [declaration.split(":", 1)]
                 if key.strip().lower() == property_name), "")


def seek_card_fields(anchor):
    """SEEK's linked table card: one emphasized title row, then employer row.

    Inspect inside the link, never a neighboring card. Layout tables can be
    deeply nested; font-size:0 on a layout cell does not hide its descendants.
    This is structure recognition, not a job-keyword filter.
    """
    try:
        if platform_for_host(urlparse(unwrap_target(anchor.get("href", ""))).hostname or "") != "SEEK":
            return None
    except (ValueError, TypeError):
        return None
    if not anchor.find("table") or anchor.find("a") or _style(anchor, "display") != "block":
        return None
    title_cells = [cell for cell in anchor.find_all("td")
                   if _style(cell, "font-weight") in {"bold", "600", "700", "800", "900"}
                   and any(_style(n, "text-decoration") == "underline" for n in cell.find_all("div"))]
    if len(title_cells) != 1:
        return None
    cell = title_cells[0]
    row = cell.parent
    if row.name != "tr" or len(row.find_all("td", recursive=False)) != 1:
        return None
    employer_row = row.find_next_sibling("tr")
    employers = employer_row.find_all("td", recursive=False) if employer_row else []
    if len(employers) != 1 or employers[0].find("table"):
        return None
    title = cell.get_text(" ", strip=True)
    company = employers[0].get_text(" ", strip=True)
    strings = list(anchor.stripped_strings)
    if not title or not company or strings[:2] != [title, company] or len(strings) < 3:
        return None
    return title, company


TEXT_URL = re.compile(r'https?://[^\s<>\[\]"()]+')


def text_links(text):
    """Tokenize bracketed URLs and image/link pairs without retaining markup.

    Context ends at the previous URL; a prior job cannot donate its title to
    the following URL. Full context is used only for exact MIME card matching.
    """
    previous_end = 0
    image_label = None
    for match in TEXT_URL.finditer(text or ""):
        adjacent_image = image_label is not None and text[previous_end:match.start()] == "]"
        context = text[previous_end:match.start()].strip(" \n\r\t[]()")
        previous_end = match.end()
        lines = [line.strip(" []()") for line in context.splitlines() if line.strip(" []()")]
        label = image_label if adjacent_image else (lines[-1] if lines else "")
        # [image URL]destination URL is an inline image link, not two jobs.
        image = text[max(0, match.start() - 1):match.start()] == "[" and bool(
            re.match(r"\]https?://", text[match.end():]))
        image_label = label if image else None
        yield dict(href=match.group().rstrip(".,;)>"), label=label, context=context,
                   image=image, bracketed=text[max(0, match.start() - 1):match.start()] == "[", anchor=None, card=None)


def _seek_control(label):
    # Finite UI patterns, never substrings of signed tracker tokens. These
    # labels do not authorize requests to any additional domain.
    if re.fullmatch(r"(?:SEEK|(?:www\.)?seek\.(?:co\.nz|com(?:\.au)?|nz)/?|(?:apple|app) store|google play|(?:edit|manage) (?:this |your )?alert|unsubscribe from (?:this |all )?alerts?|view all(?: [\d,]+)?(?: matching)? jobs)", label, re.I):
        return "navigation"
    if re.fullmatch(r"[a-z0-9.-]+/?", label, re.I) and platform_for_host(label.lower().rstrip("/")) == "SEEK":
        return "navigation"
    domains = re.findall(r"[\w.-]+\.(?:com(?:\.au)?|co\.nz)", label.lower())
    if (any(platform_for_host(d) == "SEEK" for d in domains)
            and re.search(r"\b(?:receiv\w*|sent)\b", label, re.I)
            and re.search(r"\b(?:register\w*|subscrib\w*)\b", label, re.I)
            and not JOB_ROLE.search(label)):
        return "navigation"
    if "©" in label and re.search(r"\bSEEK\b", label, re.I) and not JOB_ROLE.search(label):
        return "navigation"
    if re.fullmatch(r"explore now", label, re.I):
        return "marketing"
    if (len(label) <= 140 and re.search(r"\byour saved search results\b", label, re.I)
            and re.search(r"relevant|personal|refine|customi[sz]e|tips|fine.?tune", label, re.I)
            and not JOB_ROLE.search(label)):
        return "marketing"
    return None


def job_card(anchor):
    """Smallest semantic card, growing table rows only while evidence stays singular."""
    if seek_card_fields(anchor):
        return anchor
    chosen = anchor
    for parent in list(anchor.parents)[:8]:
        if parent.name in {"body", "html", "[document]", "footer", "nav"}:
            break
        if len(parent.get_text(" ", strip=True)) > 900:
            break
        ids = {r[:2] for a in parent.find_all("a", href=True) if (r := identify_job_url(a.get("href", "")))}
        headings = [n for n in parent.select(TITLE_SELECTOR) if title_value(n.get_text(" ", strip=True))]
        # Count leaf title nodes, not both a heading and its nested styled span.
        headings = [n for n in headings if not any(other is not n and any(a is n for a in other.parents) for other in headings)]
        titles = {n.get_text(" ", strip=True).casefold() for n in headings}
        titles.update(anchor_label(a).casefold() for a in parent.find_all("a", href=True)
                      if title_value(anchor_label(a)) and (identify_job_url(a.get("href", "")) or JOB_ROLE.search(anchor_label(a))))
        nested_cards = [n for n in parent.find_all(["article", "li", "div", "table", "tr"]) if explicit_card(n)]
        # Separate linked rows without an explicit card boundary are ambiguous;
        # don't borrow a canonical job from the neighboring row.
        linked_rows = [row for row in parent.find_all("tr", recursive=False) if row.find("a", href=True)]
        if parent.name == "table":
            linked_rows += [row for body in parent.find_all("tbody", recursive=False)
                            for row in body.find_all("tr", recursive=False) if row.find("a", href=True)]
        linked_blocks = [node for node in parent.find_all(["div", "td", "li", "table"], recursive=False) if node.find("a", href=True)]
        split_rows = (len(linked_rows) > 1 or len(linked_blocks) > 1) and bool(ids) and not explicit_card(parent)
        if len(ids) > 1 or len(headings) > 1 or len(titles) > 1 or nested_cards or split_rows:
            break
        if titles or ids:
            chosen = parent
        if explicit_card(parent):
            return parent if titles or ids else anchor
    return chosen


def redirect_rejection(url, platform):
    try:
        parsed = urlparse(url)
        if parsed.scheme != "https":
            return "reject_non_https"
        if parsed.username is not None or parsed.password is not None:
            return "reject_userinfo"
        if parsed.port not in (None, 443):
            return "reject_port"
        destination = platform_for_host((parsed.hostname or "").lower())
        if destination == "Unknown":
            return "reject_external_domain"
        if destination != platform:
            return "reject_cross_platform"
        if identify_job_url(url):
            return None
        if NAV_PATH.search(parsed.path):
            return "reject_navigation_path"
        if not TRACK_PATH.match(parsed.path):
            return "reject_unsupported_path"
    except (ValueError, TypeError):
        return "reject_malformed_url"
    return None


class RedirectResolver:
    """HEAD-only first-party redirects. Never request job pages or loosen budgets."""
    def __init__(self):
        self.cache = {}
        self.remaining = 24
        self.deadline = time.monotonic() + 45
        self.requests = 0
        self.cache_hits = 0

    def resolve(self, href, platform):
        current = link_key(href)
        key = (current, platform)
        visited = set()
        result, reason = None, "redirect_hop_limit"
        for hop in range(5):
            if (current, platform) in self.cache:
                self.cache_hits += 1
                result, reason = self.cache[(current, platform)]
                break
            reason = redirect_rejection(current, platform)
            if reason:
                break
            job = identify_job_url(current)
            if job:
                result, reason = job, "resolved"
                break
            if current in visited:
                reason = "redirect_loop"
                break
            if hop == 4:
                reason = "redirect_hop_limit"
                break
            if self.remaining <= 0:
                reason = "redirect_request_budget"
                break
            if time.monotonic() >= self.deadline:
                reason = "redirect_time_budget"
                break
            visited.add(current)
            try:
                parsed = urlparse(current)
                addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
                if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                    reason = "reject_nonpublic_dns"
                    break
            except Exception:
                reason = "redirect_dns_failed"
                break
            try:
                self.remaining -= 1
                self.requests += 1
                with requests.Session() as session:
                    session.trust_env = False
                    response = session.head(current, allow_redirects=False, timeout=4)
                    try:
                        status = response.status_code
                        location = response.headers.get("Location", "")
                        if status not in {301, 302, 303, 307, 308}:
                            reason = ("redirect_access_denied" if status in {401, 403} else
                                      "redirect_rate_limited" if status == 429 else
                                      "redirect_head_unsupported" if status == 405 else
                                      "redirect_server_error" if status >= 500 else "redirect_not_http")
                            break
                        if not location:
                            reason = "redirect_missing_location"
                            break
                        current = link_key(urljoin(current, location))
                    finally:
                        response.close()
            except Exception:
                reason = "redirect_request_failed"
                break
        # Shared intermediate hops are cached as well as starting wrappers.
        for url in visited | {key[0]}:
            self.cache[(url, platform)] = (result, reason)
        return result, reason


def prepare_links(html, text, platform, resolver, counter):
    soup = _soup(html)
    tasks = []
    replacements = {}
    groups = {}
    cards = []
    for anchor in soup.find_all("a", href=True):
        fields = seek_card_fields(anchor) if platform == "SEEK" else None
        if fields:
            cards.append((anchor, fields))
        tasks.append(dict(href=anchor.get("href", ""), label=anchor_label(anchor), anchor=anchor, card=job_card(anchor)))
    counter["structured_job_cards"] += len(cards)
    mime_targets = {}
    card_titles = {fields[0] for _, fields in cards}
    image_labels = {img.get("alt", "").strip() for img in soup.find_all("img")
                    if img.get("alt", "").strip() and not JOB_ROLE.search(img.get("alt", ""))
                    and img.get("alt", "").strip() not in card_titles
                    and (not img.find_parent("a") or seek_card_fields(img.find_parent("a")))}
    for task in text_links(text):
        # Both title AND employer must occur consecutively in this URL's own
        # text block, uniquely identifying an HTML card. Never pair by order.
        lines = [re.sub(r"\s+", " ", line.strip()) for line in task["context"].splitlines() if line.strip()]
        pairs = set(zip(lines, lines[1:]))
        matches = [(anchor, fields) for anchor, fields in cards if fields in pairs]
        if len(matches) == 1 and not task["image"]:
            anchor, fields = matches[0]
            task["card"] = anchor
            task["label"] = fields[0]
            direct = identify_job_url(task["href"])
            if direct and direct[0] == platform:
                mime_targets.setdefault(id(anchor), set()).add(direct)
        if platform == "SEEK" and task["card"] is None and task["bracketed"] and task["label"] in image_labels:
            task["image"] = True
        tasks.append(task)
    counter["link_occurrences"] += len(tasks)
    for task in tasks:
        key = link_key(task["href"])
        groups.setdefault(key, []).append(task)
        bump(counter, "url_patterns", url_pattern(task["href"]))
    counter["unique_link_targets"] += len(groups)
    counter["duplicate_links"] += len(tasks) - len(groups)

    def classification(task):
        href, label, anchor, card = (task[k] for k in ("href", "label", "anchor", "card"))
        direct = identify_job_url(href)
        if direct:
            return "canonical_job" if direct[0] == platform else "cross_platform_job"
        if platform == "SEEK" and task.get("image"):
            return "decorative_image"
        if card is not None and seek_card_fields(card):
            return "job_card"
        if platform == "SEEK" and (control := _seek_control(label)):
            return control
        if NAV_LABEL.fullmatch(label):
            return "navigation"
        if MARKETING_LABEL.fullmatch(label):
            if card is not None and card is not anchor and JOB_ROLE.search(card.get_text(" ", strip=True)):
                return "card_link"
            return "marketing"
        try:
            if urlparse(href).scheme in {"mailto", "tel"}:
                return "navigation"
            if NAV_PATH.search(urlparse(unwrap_target(href)).path):
                return "navigation"
        except (ValueError, TypeError):
            pass
        if JOB_ROLE.search(label) and title_value(label):
            return "job_title"
        if JOB_CTA.fullmatch(label):
            return "job_cta"
        if card is not None and card is not anchor and (JOB_ROLE.search(card.get_text(" ", strip=True)) or CARD_CLASS.search(" ".join(card.get("class", []))) or card.has_attr("data-job-id")):
            return "card_link"
        if "tracker" in url_pattern(href) or url_pattern(href) == "unsafe_url":
            return "ambiguous_tracking"
        if re.search(r"/jobs?/", href, re.I):
            return "unsupported_job_content"
        return "other"

    def known_in_card(task):
        card = task["card"]
        if card is None or (card is task["anchor"] and not seek_card_fields(card)):
            return None
        anchors = [card] if card.name == "a" else card.find_all("a", href=True)
        known = {r[1]: r for a in anchors
                 if (r := identify_job_url(a.get("href", ""))) and r[0] == platform}
        for r in mime_targets.get(id(card), set()):
            known[r[1]] = r
        return next(iter(known.values())) if len(known) == 1 else None

    def replace(task, job):
        if task["anchor"] is not None:
            task["anchor"]["href"] = job[2]
        else:
            replacements[task["href"]] = job[2]
        if task["card"] is not None and seek_card_fields(task["card"]):
            if mime_targets.get(id(task["card"])):
                counter["mime_card_reuse"] += 1
            task["card"]["href"] = job[2]

    conflicting_cards = set()
    for anchor, _ in cards:
        identities = {r[1] for r in mime_targets.get(id(anchor), set())}
        if direct := identify_job_url(anchor.get("href", "")):
            identities.add(direct[1])
        if len(identities) > 1:
            conflicting_cards.add(id(anchor))
    priority = {"job_card": 0, "job_title": 0, "job_cta": 1, "card_link": 2, "unsupported_job_content": 3, "cross_platform_job": 3, "ambiguous_tracking": 4}
    pending = []
    missing = bool(conflicting_cards)
    if conflicting_cards:
        for _ in conflicting_cards:
            bump(counter, "failure_reasons", "ambiguous_card_identity")
    card_failures = {}
    for key, items in groups.items():
        unresolved = []
        kinds = [classification(task) for task in items]
        card_targets = {job[2]: job for task, kind in zip(items, kinds) if kind in {"job_card", "job_title", "job_cta", "card_link"} and (job := known_in_card(task))}
        shared_card_target = next(iter(card_targets.values())) if len(card_targets) == 1 else None
        inherited_navigation = bool(set(kinds) & {"navigation", "marketing"}) and set(kinds) <= {"navigation", "marketing", "ambiguous_tracking", "other"}
        for task, kind in zip(items, kinds):
            if inherited_navigation and kind in {"ambiguous_tracking", "other"}:
                kind = "navigation"
            task["kind"] = kind
            bump(counter, "link_decisions", kind)
            if id(task["card"]) in conflicting_cards:
                continue
            if kind in {"canonical_job", "navigation", "marketing", "other", "decorative_image"}:
                continue
            known = shared_card_target if kind in {"job_card", "job_title", "job_cta", "card_link", "ambiguous_tracking"} and (task["card"] is None or known_in_card(task) == shared_card_target) else None
            if known:
                replace(task, known)
                counter["canonical_card_reuse"] += 1
            else:
                unresolved.append(task)
        if unresolved:
            pending.append((min(priority[t["kind"]] for t in unresolved), key, unresolved))
    # Strong cards first; original document order breaks ties deterministically.
    for rank, key, items in sorted(pending, key=lambda row: row[0]):
        # Another link in this card may have resolved since the first pass.
        current_targets = {job[2]: job for task in items if (job := known_in_card(task))}
        if len(current_targets) > 1:
            missing = True
            counter["unsupported_url_patterns"] += 1
            counter["unresolved_job_targets"] += 1
            bump(counter, "failure_reasons", "ambiguous_card_identity")
            continue
        known = next(iter(current_targets.values())) if current_targets else None
        if known:
            remaining_items = []
            for task in items:
                if task["card"] is None or known_in_card(task) == known:
                    replace(task, known)
                    counter["canonical_card_reuse"] += 1
                else:
                    remaining_items.append(task)
            if not remaining_items:
                continue
            items = remaining_items
        ambiguous = rank == priority["ambiguous_tracking"]
        failed_cards = {id(task["card"]) for task in items if id(task["card"]) in card_failures}
        if failed_cards and all(id(task["card"]) in card_failures for task in items):
            result, reason = None, "card_resolution_deferred"
        elif ambiguous:
            result, reason = None, "insufficient_job_evidence"
            counter["ambiguous_targets"] += 1
        elif resolver is None:
            result, reason = None, "resolution_unavailable"
        else:
            requests_before, hits_before = resolver.requests, resolver.cache_hits
            result, reason = resolver.resolve(key, platform)
            counter["redirect_requests"] += resolver.requests - requests_before
            counter["redirect_cache_hits"] += resolver.cache_hits - hits_before
        if result:
            for task in items:
                replace(task, result)
        else:
            missing = True
            for task in items:
                if task["card"] is not None and seek_card_fields(task["card"]):
                    card_failures[id(task["card"])] = reason
            counter["unsupported_url_patterns"] += 1
            counter["unresolved_job_targets"] += int(not ambiguous)
            bump(counter, "failure_reasons", reason)
    counter["unresolved_job_cards"] += sum(id(anchor) in conflicting_cards or not identify_job_url(anchor.get("href", ""))
                                           or not _valid_title(fields[0], minimum=2) for anchor, fields in cards)
    text = TEXT_URL.sub(lambda match: replacements.get(match.group().rstrip(".,;)>"), match.group()), text)
    return str(soup), text, missing


def _card(anchor):
    return job_card(anchor)


def _valid_title(value: str, minimum: int = 7) -> bool:
    return minimum <= len(value) <= 130 and not CTA.match(value) and not re.search(r"https?://|\S+@\S+", value)


def _best_title(anchor) -> tuple[str, str]:
    if fields := seek_card_fields(anchor):
        return fields
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
        if not _valid_title(title, minimum=2 if seek_card_fields(anchor) else 7):
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
    for task in text_links(text):
        if task["image"]:
            continue
        result = identify_job_url(task["href"])
        if not result:
            continue
        platform, ident, url = result
        candidate = task["label"]
        # Multiline SEEK card metadata is not a title. Its exact title/company
        # pairing is handled against the structured HTML, never guessed here.
        lines = [line.strip() for line in task["context"].splitlines() if line.strip()]
        if len(lines) > 1 and not JOB_ROLE.search(candidate):
            continue
        if title_value(candidate):
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
    decisions_before = dict(counter["link_decisions"])
    recognized = _is_job_alert(subject, sender)
    counter["alerts_recognized"] += int(recognized)
    try:
        # Do not issue tracking requests merely because a sender is a job board.
        html, text, unresolved = prepare_links(html, text, platform, resolver, counter)
        html_jobs = extract_jobs_from_html(html)
        text_jobs = extract_jobs_from_text(text)
        by_id = {(job.source, job.source_id): job for job in text_jobs + html_jobs}
        jobs = [j for j in by_id.values() if j.source == f"{platform} email alert"]
        hrefs = [a.get("href", "") for a in _soup(html).find_all("a", href=True)] + [t["href"] for t in text_links(text) if not t["image"]]
        supported = {r[1] for h in hrefs if (r := identify_job_url(h)) and r[0] == platform}
        missing_title = supported - {j.source_id for j in jobs}
        # Unsupported trackers alone do not prove marketing is a job alert.
        recognized = recognized or bool(supported) or any(counter["link_decisions"].get(k, 0) > decisions_before.get(k, 0) for k in ("job_card", "job_title", "job_cta", "unsupported_job_content", "cross_platform_job"))
        if recognized and not _is_job_alert(subject, sender):
            counter["alerts_recognized"] += 1
        counter["links_extracted"] += len(jobs)
        uncertain = not recognized and unresolved
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
