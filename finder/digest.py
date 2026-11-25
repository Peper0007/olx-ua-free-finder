"""Evening summary in Telegram: what was checked today, the best finds and the Gemini quota left."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import remote, telegram
from .common import log

SOFIA = ZoneInfo("Europe/Kyiv")  # name kept for symmetry with the BG bot
DASHBOARD = "https://olx-ua-free-finder.web.app"


def due(settings: dict, now: datetime | None = None) -> str | None:
    """Returns today's Kyiv date if the digest should go out in this run."""
    d = settings.get("digest") or {}
    if not d.get("enabled", True):
        return None
    now = (now or datetime.now(timezone.utc)).astimezone(SOFIA)
    if now.hour < int(d.get("hour", 21)):
        return None
    return now.strftime("%Y-%m-%d")


def build(settings: dict, backlog: int, quota_left: int | None) -> str:
    start = datetime.now(SOFIA).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    finds = remote.finds_since(start)
    runs = remote.runs_since(start)
    min_score = int(settings.get("min_score", 6))
    hits = [f for f in finds if (f.get("score") or 0) >= min_score and f.get("status") not in ("spam", "skip")]
    hits.sort(key=lambda f: f.get("score") or 0, reverse=True)
    e = html.escape
    lines = [f"🌙 <b>Итоги дня</b> · {datetime.now(SOFIA):%d.%m}",
             f"Проверок: {len(runs)} · оценено ИИ: {len(finds)} · находок ≥{min_score}: <b>{len(hits)}</b>"]
    if hits:
        lines.append("")
        lines.append("🏆 Лучшее за день:")
        for f in hits[:3]:
            price = f.get("price_ebay") or f.get("price") or ""
            lines.append(f'⭐ {f.get("score")}/10 · <a href="{e(f.get("url", ""))}">{e((f.get("title") or "")[:70])}</a>'
                         + (f" · {e(str(f.get('location') or '').split(',')[0].strip())}" if f.get("location") else "")
                         + (f" · eBay {e(price)}" if price and price != "—" else ""))
    else:
        lines.append("Стоящих находок сегодня не было.")
    tail = []
    if quota_left is not None:
        tail.append(f"Gemini: осталось ~{quota_left} запросов до 10:00")
    if backlog:
        tail.append(f"в очереди {backlog}")
    if tail:
        lines.append("")
        lines.append(" · ".join(tail))
    lines.append(f'<a href="{DASHBOARD}">Открыть дашборд</a>')
    return "\n".join(lines)


def maybe_send(settings: dict, backlog: int, quota_left: int | None) -> None:
    day = due(settings)
    if not day or not remote.claim_digest(day):
        return
    try:
        telegram.send_text(build(settings, backlog, quota_left), silent=True)
        log.info("Evening digest sent")
    except Exception as exc:
        log.warning("Digest failed: %s", exc)
