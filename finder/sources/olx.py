"""OLX.ua — the "Віддам безкоштовно" (free) section.

The listing page embeds all ads as JSON in `window.__PRERENDERED_STATE__`,
so no HTML scraping and no use of /api/ (which robots.txt disallows).
"""
from __future__ import annotations

import json
import re

from ..common import Ad, clean, get, log

BASE = "https://www.olx.ua"
LIST_URL = BASE + "/uk/otdam-darom/?page={page}"

_STATE_RE = re.compile(r"window\.__PRERENDERED_STATE__\s*=\s*(\"(?:[^\"\\]|\\.)*\")", re.S)


def parse_listing(html: str) -> list[Ad]:
    m = _STATE_RE.search(html)
    if not m:
        raise ValueError("OLX: __PRERENDERED_STATE__ not found (layout changed or blocked)")
    state = json.loads(json.loads(m.group(1)))
    raw_ads = state.get("listing", {}).get("listing", {}).get("ads", []) or []
    ads: list[Ad] = []
    for a in raw_ads:
        price = a.get("price") or {}
        if not price.get("free", False):
            continue  # safety: only free items
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
                category_id=str((a.get("category") or {}).get("id") or ""),
                delivery=delivery,
            )
        )
    return ads


def _ts(value) -> float:
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() if value else 0.0
    except ValueError:
        return 0.0


def fetch(pages: int = 5) -> list[Ad]:
    out: list[Ad] = []
    for page in range(1, pages + 1):
        try:
            html = get(LIST_URL.format(page=page), pause=3).text
            ads = parse_listing(html)
        except Exception as exc:  # keep going with what we have
            log.warning("OLX page %s failed: %s", page, exc)
            break
        log.info("OLX page %s: %s free ads", page, len(ads))
        if not ads:
            break
        out.extend(ads)
    return out
