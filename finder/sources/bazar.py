"""Bazar.bg — all ads with the "Подарявам" (free) filter, newest first."""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

from ..common import Ad, abs_url, clean, get, log

BASE = "https://bazar.bg"
LIST_URL = BASE + "/obiavi?free=1&sort=date&page={page}"


def parse_listing(html: str) -> list[Ad]:
    soup = BeautifulSoup(html, "lxml")
    ads: list[Ad] = []
    seen: set[str] = set()
    for a in soup.select("div.listItemContainer a.listItemLink[data-id]"):
        ad_id = a.get("data-id", "").strip()
        if not ad_id or ad_id in seen:
            continue
        seen.add(ad_id)
        price = clean(a.select_one(".price").get_text(" ") if a.select_one(".price") else "")
        if price and "Подарявам" not in price:
            continue
        img = a.select_one("img")
        photo = abs_url((img.get("data-src") or img.get("src") or "") if img else "", BASE)
        title_el = a.select_one("span.title")
        ads.append(
            Ad(
                source="bazar",
                ad_id=ad_id,
                title=clean(title_el.get_text() if title_el else a.get("title")),
                url=abs_url(a.get("href", ""), BASE),
                location=clean(a.select_one(".location").get_text() if a.select_one(".location") else ""),
                posted=clean(a.select_one(".date").get_text() if a.select_one(".date") else ""),
                photos=[photo] if photo else [],
                needs_details=True,
            )
        )
    return ads


def parse_details(html: str, ad: Ad) -> None:
    """Fill description and extra photos from the ad page."""
    soup = BeautifulSoup(html, "lxml")
    desc = soup.select_one('[itemprop="description"]')
    if desc:
        ad.description = clean(desc.get_text(" "))[:1500]
    else:
        for s in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(s.string or "{}")
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("@type") == "Product":
                ad.description = clean(re.sub(r"<[^>]+>", " ", data.get("description", "")))[:1500]
                break
    photos: list[str] = []
    for img in soup.select("img"):
        src = img.get("data-src") or img.get("src") or ""
        if "/bazar/" in src and "/pics/" in src:
            u = abs_url(src, BASE)
            if u not in photos:
                photos.append(u)
    if photos:
        ad.photos = photos[:4]


def fetch(pages: int = 3) -> list[Ad]:
    out: list[Ad] = []
    for page in range(1, pages + 1):
        try:
            ads = parse_listing(get(LIST_URL.format(page=page), pause=3).text)
        except Exception as exc:
            log.warning("bazar page %s failed: %s", page, exc)
            break
        log.info("bazar page %s: %s free ads", page, len(ads))
        if not ads:
            break
        out.extend(ads)
    return out


def enrich(ad: Ad) -> None:
    try:
        parse_details(get(ad.url, pause=2).text, ad)
    except Exception as exc:
        log.warning("bazar details failed for %s: %s", ad.url, exc)
