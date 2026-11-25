"""OLX.ua — the "Віддам безкоштовно" (free) section, plus cheap ads (≤ N грн) in chosen categories.

The listing page embeds all ads as JSON in `window.__PRERENDERED_STATE__`,
so no HTML scraping and no use of /api/ (which robots.txt disallows).
"""
from __future__ import annotations

import json
import re

from ..common import Ad, clean, get, log, stop_when_seen

BASE = "https://www.olx.ua"
LIST_URL = BASE + "/uk/otdam-darom/?page={page}"
# cheap ads in a category: price filter in UAH, OLX sorts by last bump (newest first)
CHEAP_URL = BASE + "/uk/{cat}/?search%5Bfilter_float_price%3Ato%5D={max}&page={page}"

_STATE_RE = re.compile(r"window\.__PRERENDERED_STATE__\s*=\s*(\"(?:[^\"\\]|\\.)*\")", re.S)


def parse_listing(html: str, max_price: float | None = None) -> list[Ad]:
    """Free ads only; with max_price also paid ads costing up to max_price UAH."""
    m = _STATE_RE.search(html)
    if not m:
        raise ValueError("OLX: __PRERENDERED_STATE__ not found (layout changed or blocked)")
    state = json.loads(json.loads(m.group(1)))
    raw_ads = state.get("listing", {}).get("listing", {}).get("ads", []) or []
    ads: list[Ad] = []
    for a in raw_ads:
        price = a.get("price") or {}
        value = 0.0
        if not price.get("free", False):
            if max_price is None:
                continue  # safety: only free items
            try:
                value = float(((price.get("regularPrice") or {}).get("value")))
            except (TypeError, ValueError):
                continue  # «exchange», «по договаряне» etc.
            if value <= 0 or value > max_price:
                continue
        loc = a.get("location") or {}
        location = ", ".join(
            x for x in (loc.get("cityName"), loc.get("districtName"), loc.get("regionName")) if x
        ) or loc.get("pathName") or ""
        delivery = "OLX Доставка" if ((a.get("delivery") or {}).get("rock") or {}).get("active") else ""
        for p in a.get("params") or []:
            if p.get("key") == "delivery_paid_by":
                delivery = delivery or clean(str(p.get("value", "")))
        photos = [p for p in (a.get("photos") or []) if isinstance(p, str)][:4]
        ads.append(
            Ad(
                source="olx",
                ad_id=str(a.get("id")),
                title=clean(a.get("title")),
                url=(a.get("url") or "").split("?")[0],
                location=clean(location),
                description=clean(a.get("description"))[:1500],
                photos=photos,
                posted=(a.get("createdTime") or "")[:16].replace("T", " "),
                posted_at=_ts(a.get("createdTime")),
                delivery=delivery,
                price=value,
                seller=str((a.get("user") or {}).get("id") or ""),
                category_id=str((a.get("category") or {}).get("id") or ""),
            )
        )
    return ads


def _ts(value) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() if value else 0.0
    except ValueError:
        return 0.0


def _pages(url_tpl: str, pages: int, label: str, **fmt):
    for page in range(1, pages + 1):
        try:
            html = get(url_tpl.format(page=page, **fmt), pause=2).text
            ads = parse_listing(html, fmt.get("max"))
        except Exception as exc:  # keep going with what we have
            log.warning("%s page %s failed: %s", label, page, exc)
            return
        log.info("%s page %s: %s ads", label, page, len(ads))
        if not ads:
            return
        yield ads


def fetch(pages: int = 25, seen=None) -> list[Ad]:
    return stop_when_seen(_pages(LIST_URL, pages, "OLX"), seen, "OLX")


def fetch_cheap(categories: list[str], max_uah: float, pages: int = 3, seen=None) -> list[Ad]:
    out: list[Ad] = []
    for cat in categories or []:
        cat = cat.strip().strip("/")
        if not cat:
            continue
        label = f"OLX {cat} ≤{max_uah:g} грн"
        out += stop_when_seen(_pages(CHEAP_URL, pages, label, cat=cat, max=max_uah), seen, label)
    return out
