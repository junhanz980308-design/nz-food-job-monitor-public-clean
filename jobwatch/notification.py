"""Telegram notifications; never log tokens or include email content."""
from __future__ import annotations

import html
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from .matching import Match
from .models import Job, now_utc


def localtime(value: str) -> str:
    if not value:
        return "未知"
    try:
        iso = value.replace("Z", "+00:00")
        date = datetime.fromisoformat(iso)
        if not date.tzinfo:
            return value + " (time zone unverified)"
        return date.astimezone(ZoneInfo("Asia/Tokyo")).strftime("%Y-%m-%d %H:%M JST")
    except (ValueError, OverflowError):
        return value[:40]


def job_message(job: Job, match: Match, first_seen: str | None = None) -> str:
    esc = lambda value: html.escape(str(value or ""))
    title = esc(job.title)
    source = '<a href="https://www.adzuna.co.nz/">The Adzuna API</a>' if job.source == "Adzuna" else esc(job.source)
    reason = ", ".join(match.reasons) or "职位涉及相关食品研究或开发"
    published_line = (
        f"📅 <b>{esc(job.published_label)}:</b> {esc(localtime(job.published_at))}\n"
        if job.published_at else "📅 <b>原始发布日期:</b> 未核实\n"
    )
    return (
        f"🚨 <b>新西兰食品岗位提醒 | {esc(match.tier)}</b>\n\n"
        f"<b>{title}</b>\n"
        f"🏢 {esc(job.company or '雇主未从来源识别')}\n"
        f"📍 {esc(job.location or '地点待核实')}\n"
        f"🗂 来源: {source}\n"
        f"🎯 匹配评分: {match.score}/100（规则评分，仅供筛选）\n"
        f"🔎 匹配原因: {esc(reason)}\n"
        f"{published_line}"
        f"⏰ 首次发现: {esc(localtime(first_seen or now_utc()))}\n"
        f"💰 薪资: {esc(match.salary_flag)}\n"
        f"🛂 工作许可: {esc(match.rights_flag)}\n"
        f"🗓 截止日期: {esc(job.deadline or '未公开')}\n"
        f"🔗 <a href=\"{esc(job.url)}\">查看职位或申请</a>\n\n"
        "提示：部分综合平台和邮件提醒存在索引延迟；请核对官方招聘页。"
    )


def send_message(message: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("Telegram secrets missing: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=(8, 20))
            if r.status_code == 429:
                try:
                    retry = max(0, min(30, int(r.json().get("parameters", {}).get("retry_after", 3))))
                except (ValueError, TypeError, AttributeError):
                    retry = 3
                time.sleep(retry)
                continue
            try:
                data = r.json()
            except ValueError:
                raise RuntimeError("Telegram returned invalid JSON") from None
            if r.status_code != 200 or not isinstance(data, dict) or not data.get("ok"):
                raise RuntimeError(f"Telegram rejected request (HTTP {r.status_code})")
            return
        except requests.exceptions.RequestException:
            if attempt == 2:
                raise RuntimeError("Telegram network request failed") from None
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("Telegram rate limiting did not clear")
