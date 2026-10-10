"""Public/authorized job sources. The job boards SEEK/LinkedIn use email alerts, not scraping."""
from __future__ import annotations

import logging
import os
import re
from html import unescape
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .models import Job, SourceResult, is_nz_location

LOG = logging.getLogger(__name__)
USER_AGENT = "NZFoodJobWatch/1.0 (personal job search; public feeds only)"


class SourceError(Exception):
    pass


def get_json(url: str, *, params: dict | None = None) -> dict | list:
    """Safe error messages: avoid printing secret query-string credentials."""
    try:
        response = requests.get(
            url, params=params,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            timeout=(8, 25),
        )
        if response.status_code != 200:
            raise SourceError(f"HTTP {response.status_code}")
        return response.json()
    except requests.exceptions.RequestException as exc:
        raise SourceError(f"Network error ({type(exc).__name__})") from None
    except ValueError:
        raise SourceError("Endpoint did not return JSON") from None


def text_only(value: str | None) -> str:
    return BeautifulSoup(unescape(value or ""), "html.parser").get_text(" ", strip=True)


def records(payload, key: str) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get(key), list):
        raise SourceError("Unexpected API response shape")
    result = payload[key]
    if any(not isinstance(item, dict) for item in result):
        raise SourceError("Unexpected job record shape")
    return result


def adzuna_search(query: str) -> SourceResult:
    source_id = f"adzuna:{query}"
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        return SourceResult(source_id, [], "PENDING", "Set ADZUNA_APP_ID and ADZUNA_APP_KEY")
    payload = get_json(
        "https://api.adzuna.com/v1/api/jobs/nz/search/1",
        params={
            "app_id": app_id, "app_key": app_key, "what": query,
            "results_per_page": 50, "max_days_old": 7,
            "sort_by": "date", "content-type": "application/json",
        },
    )
    output: list[Job] = []
    for item in records(payload, "results"):
        title = text_only(item.get("title"))
        company = (item.get("company") or {}).get("display_name", "")
        loc = (item.get("location") or {}).get("display_name", "")
        salary = ""
        low, high = item.get("salary_min"), item.get("salary_max")
        if low or high:
            salary = f"NZD {low:,.0f}–{high:,.0f} / year (Adzuna may estimate)" if low and high else f"NZD {low or high:,.0f} (Adzuna data; verify with employer)"
        output.append(Job(
            source="Adzuna", source_id=str(item.get("id", "")), title=title,
            company=company, location=loc, description=text_only(item.get("description")),
            url=item.get("redirect_url", ""), salary=salary,
            # Adzuna "created" need not be original employer publication time.
            published_at=item.get("created", ""),
            published_label="Adzuna listing date (not verified original publication)",
        ))
    return SourceResult(source_id, output, "LIVE", f"{len(output)} listings returned; first 50 only, seven-day window")


def greenhouse(name: str, token: str, nz_only: bool) -> SourceResult:
    source_id = f"greenhouse:{name}"
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{quote(token, safe='')}/jobs", params={"content": "true"})
    jobs: list[Job] = []
    for item in records(data, "jobs"):
        loc = (item.get("location") or {}).get("name", "")
        if nz_only and not is_nz_location(loc):
            continue
        jobs.append(Job(
            "Greenhouse", str(item.get("id", "")), item.get("title", ""), name, loc,
            text_only(item.get("content", "")), item.get("absolute_url", ""),
            metadata={"updated_at": item.get("updated_at", "")},
        ))
    return SourceResult(source_id, jobs, "LIVE", f"{len(jobs)} NZ/uncertain-location postings")


def lever(name: str, token: str, nz_only: bool) -> SourceResult:
    source_id = f"lever:{name}"
    jobs: list[Job] = []
    # Lever publishes paginated JSON. Cap to prevent unbounded querying.
    for skip in range(0, 2000, 100):
        data = get_json(f"https://api.lever.co/v0/postings/{quote(token, safe='')}", params={"mode": "json", "limit": 100, "skip": skip})
        if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
            raise SourceError("Unexpected Lever response shape")
        for item in data:
            loc = (item.get("categories") or {}).get("location", "")
            country = item.get("country")
            if nz_only and ((country and country.upper() != "NZ") or not is_nz_location(loc)):
                continue
            description = " ".join(str(item.get(k) or "") for k in ("descriptionPlain", "additionalPlain", "description"))
            jobs.append(Job(
                "Lever", str(item.get("id", "")), item.get("text", ""), name, loc,
                text_only(description), item.get("applyUrl") or item.get("hostedUrl", ""),
                metadata={"createdAt": item.get("createdAt")},
            ))
        if len(data) < 100:
            break
    else:
        raise SourceError("Lever page limit exceeded; incomplete scan discarded")
    return SourceResult(source_id, jobs, "LIVE", f"{len(jobs)} NZ/uncertain-location postings")


def ashby(name: str, token: str, nz_only: bool) -> SourceResult:
    source_id = f"ashby:{name}"
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{quote(token, safe='')}", params={"includeCompensation": "true"})
    jobs: list[Job] = []
    for item in records(data, "jobs"):
        if item.get("isListed") is False:
            continue
        loc = item.get("location", "")
        country = ((item.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        if nz_only and ((country and country.upper() not in {"NZ", "NEW ZEALAND"}) or not is_nz_location(loc)):
            continue
        comp = item.get("compensation", {}) or {}
        salary = comp.get("compensationTierSummary", "") if isinstance(comp, dict) else ""
        jobs.append(Job(
            "Ashby", str(item.get("id", "")), item.get("title", ""), name, loc,
            text_only(item.get("descriptionPlain") or item.get("descriptionHtml")),
            item.get("applyUrl") or item.get("jobUrl", ""),
            published_at=item.get("publishedAt") or "",
            published_label="Ashby last published time (could include reposting)",
            salary=salary,
        ))
    return SourceResult(source_id, jobs, "LIVE", f"{len(jobs)} NZ/uncertain-location postings")


def smartrecruiters(name: str, token: str, nz_only: bool) -> SourceResult:
    source_id = f"smartrecruiters:{name}"
    output: list[Job] = []
    base = f"https://api.smartrecruiters.com/v1/companies/{quote(token, safe='')}/postings"
    for offset in range(0, 2000, 100):
        params = {"limit": 100, "offset": offset, "destination": "PUBLIC"}
        if nz_only:
            params["country"] = "nz"
        response = get_json(base, params=params)
        items = records(response, "content")
        for item in items:
            location = item.get("location") or {}
            loc = ", ".join(str(location.get(k) or "") for k in ("city", "region", "country")).strip(", ")
            country = location.get("country")
            if nz_only and ((country and country.upper() != "NZ") or not is_nz_location(loc)):
                continue
            # Only retrieve details for potentially relevant titles to conserve employer API requests.
            title = item.get("name", "")
            if not re.search(r"food|research|scient|technolog|product|formul|npd|dairy|sensory|flavou?r|analytic|lab|process|ingredient", title, re.I):
                continue
            detail = get_json(f"{base}/{quote(str(item.get('id', '')), safe='')}")
            sections = (detail.get("jobAd") or {}).get("sections") or {}
            desc = " ".join(text_only((v or {}).get("text", "")) for v in sections.values() if isinstance(v, dict))
            url = detail.get("applyUrl") or detail.get("jobAdUrl") or item.get("applyUrl") or item.get("jobAdUrl") or ""
            output.append(Job(
                "SmartRecruiters", str(item.get("id", "")), title, name, loc, desc,
                url, published_at=item.get("releasedDate") or "",
                published_label="SmartRecruiters released date",
            ))
        if len(items) < 100 or offset + len(items) >= response.get("totalFound", float("inf")):
            break
    else:
        raise SourceError("SmartRecruiters page limit exceeded; incomplete scan discarded")
    return SourceResult(source_id, output, "LIVE", f"{len(output)} NZ/uncertain-location postings")


def workable(name: str, token: str, nz_only: bool) -> SourceResult:
    """Workable's documented public published-jobs endpoint; no SPI token."""
    data = get_json(f"https://www.workable.com/api/accounts/{quote(token, safe='')}", params={"details": "true"})
    output = []
    for item in records(data, "jobs"):
        location = item.get("location") or {}
        loc = location.get("location_str") or ", ".join(
            str(location.get(k) or "") for k in ("city", "region", "country")
        ).strip(", ")
        country = location.get("country_code")
        if nz_only and (country and country.upper() != "NZ" or not is_nz_location(loc)):
            continue
        if item.get("state", "published") != "published":
            continue
        output.append(Job(
            "Workable", str(item.get("shortcode") or item.get("id") or ""),
            item.get("title", ""), name, loc,
            text_only(item.get("description", "")),
            item.get("url") or item.get("application_url") or item.get("shortlink") or "",
        ))
    return SourceResult(f"workable:{name}", output, "LIVE", f"{len(output)} NZ/uncertain-location postings")


ATS_ADAPTERS = {
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
    "smartrecruiters": smartrecruiters,
    "workable": workable,
}


def poll_ats(entry: dict) -> SourceResult:
    kind, name, token = entry.get("type", ""), entry.get("name", ""), entry.get("token", "")
    sid = f"{kind}:{name}"
    if kind not in ATS_ADAPTERS:
        return SourceResult(sid, [], "RESTRICTED", "Unknown adapter type")
    if not name or not token or token.startswith("VERIFIED_") or name.startswith("REPLACE_"):
        return SourceResult(sid, [], "PENDING", "Find and validate the employer's real board token")
    return ATS_ADAPTERS[kind](name, token, entry.get("nz_only", True))


def official_feed(entry: dict) -> SourceResult:
    """Optional, explicitly configured official RSS/Atom job feed; never crawl an HTML page."""
    import feedparser
    name = str(entry.get("name", ""))
    url = str(entry.get("url", ""))
    sid = f"feed:{name}"
    if not name or not url.startswith("https://") or "example.org" in url:
        return SourceResult(sid, [], "PENDING", "Add a verified HTTPS RSS/Atom job-feed URL")
    try:
        response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(8, 25))
        if response.status_code != 200:
            raise SourceError(f"RSS endpoint HTTP {response.status_code}")
        parsed = feedparser.parse(response.content)
    except requests.exceptions.RequestException:
        raise SourceError("Feed network request failed") from None
    if parsed.get("bozo") or not parsed.get("version"):
        raise SourceError("Feed XML invalid or not an RSS/Atom endpoint")
    jobs = []
    for item in parsed.entries:
        loc = item.get("location") or ""
        if entry.get("nz_only", True) and not is_nz_location(loc):
            continue
        url = item.get("link", "")
        if not url.startswith("https://"):
            continue
        jobs.append(Job(
            source=f"Official RSS: {name}", source_id=str(item.get("id", "")) or url,
            title=text_only(item.get("title", "")), company=name, location=loc,
            description=text_only(item.get("summary", "")), url=url,
            published_at=item.get("published", ""),
            published_label="Feed published timestamp (may reflect revisions)",
        ))
    return SourceResult(sid, jobs, "LIVE", f"{len(jobs)} feed entries")
