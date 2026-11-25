"""«Is the ad still online?» — re-checks open finds so removed ads leave «Находки» by themselves.

OLX answers 410 (removed) / 404; its active pages may still contain «Оголошення неактивне» in a
translations bundle, so only the status code counts. Anything unclear (network error, 403, captcha)
counts as «don't know» and changes nothing.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from .common import log, session


def is_alive(url: str) -> bool | None:
    if not url:
        return None
    try:
        resp = session().get(url, timeout=20, allow_redirects=True)
    except Exception as exc:
        log.debug("alive check failed %s: %s", url, exc)
        return None
    if resp.status_code in (404, 410):
        return False
    if resp.status_code != 200:
        return None
    return True


def check(items: list[dict], workers: int = 4) -> list[tuple[dict, bool | None]]:
    """items: [{id, url, ...}] → [(item, alive)]; a few in parallel, politely."""
    if not items:
        return []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(zip(items, pool.map(lambda it: is_alive(it["url"]), items)))
