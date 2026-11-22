"""Gemini vision filter: looks at photos + text and decides whether an ad
contains something worth reselling on Etsy / eBay."""
from __future__ import annotations

import base64
import json
import os
import time

import requests

from .common import Ad, get, log

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.5-flash-lite"  # free tier: 15 req/min, 500 req/day

PROMPT_HEAD = """Ты — опытный перекупщик винтажа. Покупаешь в Украине, продаёшь на Etsy и eBay.
Перед тобой бесплатное объявление («Віддам безкоштовно») с OLX.ua. Посмотри на ВСЕ фото и текст.

"""

PROMPT_AD = """

Объявление:
Сайт: {source}
Заголовок: {title}
Место: {location}
Доставка: {delivery}
Описание: {description}

Ответь строго JSON-объектом:
{{
  "score": целое 0-10 (10 = точно стоит забирать ради Etsy/eBay, 0 = мусор),
  "items": "что именно ценного видно (по-русски, коротко)",
  "why": "почему это продаётся, 1 предложение (по-русски)",
  "price_etsy": "реалистичная цена продажи на Etsy в USD, например '$25–35' (или '—', если не для Etsy)",
  "price_ebay": "реалистичная цена продажи на eBay в EUR, например '€20–30' (или '—', если не для eBay)",
  "etsy_ok": true или false — можно ли продавать на Etsy по ИХ правилам: только винтаж (вещи 20+ лет), handmade или материалы для рукоделия; современные фабричные вещи — false,
  "era": "примерный возраст/эпоха вещи по-русски, например '1970-е, СССР' или 'современная'",
  "query_en": "короткий поисковый запрос НА АНГЛИЙСКОМ для поиска проданных аналогов на eBay: бренд, модель, тип, эпоха — 2–6 слов, без лишнего (например 'Rotring Isograph 0.20 pen', 'Soviet porcelain figurine LFZ')",
  "pickup": "легко ли получить: 'отправит Новой почтой/OLX Доставкой' / 'только самовывоз' / 'неизвестно'"
}}"""

_last_call = 0.0
calls = 0  # HTTP requests made to Gemini in this process (counts against the daily quota)


def _image_part(url: str) -> dict | None:
    try:
        resp = get(url, pause=0.3, retries=1)
    except Exception as exc:
        log.debug("image download failed %s: %s", url, exc)
        return None
    mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/") or len(resp.content) > 4_000_000:
        return None
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(resp.content).decode()}}


def analyze(ad: Ad, *, model: str | None = None, criteria: str | None = None,
            examples: str = "", min_interval: float = 5.0) -> dict | None:
    """Returns the parsed JSON verdict, or None on failure."""
    global _last_call, calls
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    if criteria is None:
        from .remote import DEFAULT_CRITERIA
        criteria = DEFAULT_CRITERIA

    parts: list[dict] = [
        {
            "text": PROMPT_HEAD + criteria + (examples or "") + PROMPT_AD.format(
                source=ad.source,
                title=ad.title,
                location=ad.location or "?",
                delivery=ad.delivery or "?",
                description=(ad.description or "—")[:1500],
            )
        }
    ]
    for url in ad.photos[:4]:
        part = _image_part(url)
        if part:
            parts.append(part)

    body = {
        "contents": [{"role": "user", "parts": parts}],
        # No temperature/top_p: deprecated for current Gemini models.
        "generationConfig": {"responseMimeType": "application/json"},
    }

    # Respect free-tier requests-per-minute limits.
    wait = min_interval - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)

    for attempt in range(3):
        _last_call = time.time()
        calls += 1
        try:
            resp = requests.post(
                API_URL.format(model=model),
                params={"key": api_key},
                json=body,
                timeout=120,
            )
        except requests.RequestException as exc:
            log.warning("Gemini request failed (%s), attempt %s", type(exc).__name__, attempt + 1)
            time.sleep(5)
            continue
        if resp.status_code == 429:
            if "PerDay" in resp.text or "per day" in resp.text.lower():
                raise QuotaExhausted("Gemini daily quota exhausted")
            log.warning("Gemini 429 (per-minute limit), waiting %ss", 30 * (attempt + 1))
            time.sleep(30 * (attempt + 1))
            continue
        if resp.status_code >= 500:
            log.warning("Gemini %s, retrying", resp.status_code)
            time.sleep(10)
            continue
        if resp.status_code != 200:
            log.error("Gemini error %s: %s", resp.status_code, resp.text[:500])
            if resp.status_code in (400, 403, 404):
                raise RuntimeError(f"Gemini request rejected ({resp.status_code}); check model/key")
            return None
        try:
            parts_out = resp.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts_out if not p.get("thought"))
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(text)
            if isinstance(data, list):
                data = data[0] if data else {}
            data["score"] = int(data.get("score", 0))
            pe, pb = str(data.get("price_etsy") or "—"), str(data.get("price_ebay") or "—")
            data["price_etsy"], data["price_ebay"] = pe, pb
            data["query_en"] = str(data.get("query_en") or "").strip()[:100]
            ok = data.get("etsy_ok")
            data["etsy_ok"] = ok if isinstance(ok, bool) else str(ok).strip().lower() in ("true", "yes", "да", "1")
            data["era"] = str(data.get("era") or "").strip()[:60]
            data["price"] = f"Etsy {pe} · eBay {pb}"
            return data
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            log.warning("Gemini answer not parsed for %s: %s", ad.key, exc)
            return None
    return None


class QuotaExhausted(RuntimeError):
    pass
