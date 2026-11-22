"""Alo.bg — no dedicated free section, so we use the site search for
"подарявам"/"подаряваме" (first page only: robots.txt disallows search pagination)."""
from __future__ import annotations

import re
from urllib.parse import quote

from bs4 import BeautifulSoup

from ..common import Ad, abs_url, clean, get, log

BASE = "https://www.alo.bg"
SEARCH_URL = BASE + "/searchq/?q={q}"
QUERIES = ["подарявам", "подаряваме"]

_ID_RE = re.compile(r"adrows_(\d+)")
_PRICE_RE = re.compile(r"(\d[\d\s.,]*)")


def _price_value(text: str) -> float | None:
    m = _PRICE_RE.search(text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def parse_listing(html: str) -> list[Ad]:
    soup = BeautifulSoup(html, "lxml")
    ads: list[Ad] = []
    for item in soup.select("div.listvip-item, div.listtop-item"):
        m = _ID_RE.search(item.get("id", ""))
        if not m:
            continue
        text = item.get_text(" ")
        if "СУПЕР ТОП" in text or "VIP промотиране" in text:
            continue  # paid business promos, never free items
        price_el = item.select_one(".price_nowrap")
        if price_el:
            value = _price_value(price_el.get_text())
            if value is not None and value > 2:  # "1 лв." is often used for free items
                continue
        link = item.select_one("a[href]")
        href = (link.get("href") if link else "") or ""
        img = item.select_one("img")
        photo = abs_url((img.get("data-src") or img.get("src") or "") if img else "", BASE)
        if photo.endswith("_medium.jpg"):
            photo = photo.replace("_medium.jpg", "_big.jpg")
        title_el = item.select_one("h3")
        loc_el = item.select_one(".mobile_block")
        desc_el = item.select_one(".listvip-desc, .listtop-desc")
        ads.append(
            Ad(
                source="alo",
                ad_id=m.group(1),
                title=clean(title_el.get_text() if title_el else item.get("title")),
                url=abs_url(href.split("#")[0], BASE),
                location=clean(loc_el.get_text() if loc_el else ""),
                description=clean(desc_el.get_text(" ") if desc_el else "")[:1500],
                photos=[photo] if photo and "user_files" in photo else [],
            )
        )
    return ads


def fetch(queries: list[str] | None = None) -> list[Ad]:
    out: dict[str, Ad] = {}
    for q in (queries or QUERIES):
        try:
            ads = parse_listing(get(SEARCH_URL.format(q=quote(q)), pause=3).text)
        except Exception as exc:
            log.warning("alo search '%s' failed: %s", q, exc)
            continue
        log.info("alo '%s': %s ads", q, len(ads))
        for ad in ads:
            out.setdefault(ad.ad_id, ad)
    return list(out.values())
