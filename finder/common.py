"""Shared types and HTTP helpers."""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests

log = logging.getLogger("finder")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_session: requests.Session | None = None


def session() -> requests.Session:
    global _session
    if _session is None:
        s = requests.Session()
        s.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "bg-BG,bg;q=0.9,en;q=0.8",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            }
        )
        _session = s
    return _session


# Optional hook: main.py sets this to report live progress ("which page is being read") to the dashboard.
on_fetch = None


def get(url: str, *, pause: float = 2.0, retries: int = 2, **kwargs) -> requests.Response:
    """GET with a polite pause and a couple of retries on transient errors."""
    if on_fetch:
        try:
            on_fetch(url)
        except Exception:
            pass
    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = session().get(url, timeout=30, **kwargs)
            if resp.status_code in (429, 502, 503, 504) and attempt < retries:
                time.sleep(10 * (attempt + 1))
                continue
            resp.raise_for_status()
            time.sleep(pause)
            return resp
        except requests.RequestException as exc:  # network error, 4xx/5xx
            last_exc = exc
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


@dataclass
class Ad:
    source: str            # "olx"
    ad_id: str             # id within the source
    title: str
    url: str
    location: str = ""
    description: str = ""
    photos: list[str] = field(default_factory=list)
    posted: str = ""       # human-readable date/time if known
    posted_at: float = 0.0  # unix time of publication if known (OLX), 0 = unknown
    delivery: str = ""     # e.g. "купувача, лично предаване"
    needs_details: bool = False  # True if description/photos should be fetched from the ad page
    seller: str = ""       # seller id on the site, if known (used to bundle many ads of one seller into a lot)
    members: list = field(default_factory=list)  # a lot: the separate ads it was built from
    price: float = 0.0     # asking price in UAH; 0 = free («віддам безкоштовно»)
    category_id: str = ""  # OLX subcategory id (used by the category filter)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.ad_id}"


def stop_when_seen(pages_ads, seen: set | dict | None, label: str, quiet_pages: int = 2):
    """Listings are sorted newest/bumped first. Once `quiet_pages` pages in a row contain nothing
    we haven't processed yet, everything below is old too — stop paging (saves minutes per run).
    `pages_ads` yields the ads of each page; returns all collected ads."""
    out: list = []
    quiet = 0
    for ads in pages_ads:
        out.extend(ads)
        if seen is None:
            continue
        fresh = [a for a in ads if a.key not in seen]
        quiet = quiet + 1 if not fresh else 0
        if quiet >= quiet_pages:
            log.info("%s: %s pages in a row without new ads — stop paging", label, quiet)
            break
    return out


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def abs_url(url: str, base: str) -> str:
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return base.rstrip("/") + url
    if not url.startswith("http"):
        return base.rstrip("/") + "/" + url
    return url
