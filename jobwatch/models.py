"""Shared job records and stable, non-sensitive identities."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse


TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term",
    "trk", "trackingid", "ref", "refid", "source", "originalsubdomain",
    "lipi", "li_fat_id", "mc_cid", "mc_eid", "from", "campaignid",
    "clickid", "sessionid", "isfromemail", "emailtoken", "linkcode",
}
JOB_ID_PARAMS = {"jk", "jobid", "job_id", "gh_jid", "lever_job_id", "job"}


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def slug(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").lower()
    value = re.sub(r"\b(limited|ltd|incorporated|inc|pty|company|co|the)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def location_key(value: str) -> str:
    """Normalize frequent variations like 'Auckland, Auckland' for deduplication."""
    loc = slug(value)
    places = (
        "palmerston north", "new plymouth", "bay of plenty", "hawkes bay",
        "auckland", "wellington", "hamilton", "christchurch", "dunedin",
        "tauranga", "rotorua", "napier", "nelson", "invercargill",
        "whangarei", "waikato", "canterbury", "otago", "taranaki",
    )
    for place in places:
        if place in loc:
            return place
    return loc


def canonical_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url.strip())
        port = parsed.port
        if parsed.username or parsed.password or port not in (None, 80, 443):
            return ""
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    host = (parsed.hostname or "").lower()
    if not host:
        return ""
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/") or "/"
    if host == "linkedin.com" or host.endswith(".linkedin.com"):
        match = re.search(r"/jobs/view/(\d+)", path)
        if match:
            return f"https://www.linkedin.com/jobs/view/{match.group(1)}"
    if any(host == d or host.endswith('.' + d) for d in ('seek.com', 'seek.co.nz', 'seek.com.au')):
        match = re.search(r"/job/(\d+)", path)
        if match:
            return f"https://nz.seek.com/job/{match.group(1)}"
    if host == "indeed.com" or host.endswith(".indeed.com"):
        params = dict(parse_qsl(parsed.query))
        if params.get("jk"):
            return "https://nz.indeed.com/viewjob?" + urlencode({"jk": params["jk"]})
    params = [
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() in JOB_ID_PARAMS or key.lower() not in TRACKING_PARAMS
        and not key.lower().startswith(("utm_", "trk_", "tracking_"))
    ]
    return urlunparse(("https", host, path, "", urlencode(sorted(params)), ""))


@dataclass
class Job:
    source: str
    source_id: str
    title: str
    company: str = ""
    location: str = ""
    description: str = ""
    url: str = ""
    published_at: str = ""
    published_label: str = ""
    salary: str = ""
    deadline: str = ""
    work_rights: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def namespace(self) -> str:
        source = self.source.removesuffix(" email alert")
        if source in {"Greenhouse", "Lever", "Ashby", "SmartRecruiters", "Workable"}:
            source += ":" + slug(self.company)
        return digest(source)

    def tokens(self) -> list[str]:
        """Multiple aliases improve cross-platform dedupe without saving private URLs."""
        results: list[str] = []
        if self.source_id:
            results.append("id:" + digest(self.namespace() + ":" + self.source_id))
        if canonical_url(self.url):
            results.append("url:" + digest(canonical_url(self.url)))
        t, c, l = slug(self.title), slug(self.company), location_key(self.location)
        if t and c:
            # Same job advertised on several boards, usually with the same employer and title.
            if l:
                results.append("role:" + digest(f"{t}|{c}|{l}"))
        return results or ["fallback:" + digest(self.title + self.source)]


@dataclass
class SourceResult:
    source_id: str
    jobs: list[Job]
    status: str = "LIVE"
    detail: str = ""
    counters: dict[str, dict] = field(default_factory=dict)


def is_nz_location(location: str) -> bool:
    if not location.strip():
        return True  # unknown: preserve as possible match rather than silently losing it
    s = slug(location)
    if re.search(r"\b(australia|sydney|melbourne|brisbane|canberra|perth|uk|united kingdom|london|usa|united states|new york|singapore|india)\b", s):
        return False
    if re.search(r"\b(new zealand|aotearoa|auckland|wellington|hamilton|waikato|palmerston north|manawatu|christchurch|canterbury|dunedin|otago|nelson|tauranga|bay of plenty|napier|hawke|new plymouth|taranaki|invercargill|southland|rotorua|whangarei|nz)\b", s):
        return True
    if s in {"remote", "anywhere", "global", "worldwide"}:
        return False
    # Unrecognized place is uncertain; keep but highlight that the location needs review.
    return True
