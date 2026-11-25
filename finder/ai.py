"""Gemini vision filter: looks at photos + text and decides whether an ad
contains something worth reselling on Etsy / eBay.

Several ads go into ONE request (batch): the free-tier quota counts requests, not ads,
so a batch of 4 gives ~4× more checked ads per day. Requests run in parallel threads,
with request starts spaced to respect the per-minute limit."""
from __future__ import annotations

import base64
import json
import os
import threading
import time

import requests

from .common import Ad, get, log

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.5-flash-lite"  # free tier: 15 req/min, 500 req/day
# Used when the main model is overloaded (503) or out of its daily quota; each model has its own free quota.
DEFAULT_FALLBACKS = ["gemini-3.1-flash-lite", "gemini-flash-latest"]

PROMPT_HEAD = """Ты — опытный перекупщик винтажа. Покупаешь в Украине, продаёшь на Etsy и eBay.
Ниже объявления с OLX.ua: бесплатные («Віддам безкоштовно») или очень дешёвые (цена в гривнах). Посмотри на ВСЕ фото и текст.
Для платного учитывай цену покупки: интересно, только если перепродажа явно окупает её вместе с доставкой.
Каждое объявление оценивай отдельно; фото идут сразу после текста своего объявления и относятся только к нему.

КАК ОЦЕНИВАТЬ ЦЕНУ (по ней владелец решает, ехать ли за вещью, — завышать нельзя):
- называй цену, за которую такие вещи реально ПРОДАЮТСЯ (проданные лоты), а не цену выставления; при сомнении бери нижнюю границу;
- цена — за весь лот целиком в том виде, как он на фото, а не за штуку; большие лоты массовых вещей продаются со скидкой;
- массовое дёшево: обычные монеты (кроме серебра/золота и редких годов), обычные CD/DVD/кассеты, автозапчасти без номера детали,
  современные стикеры и фишки, обычная бижутерия — обычно €3–15;
- дороже стоят: ручная работа (ковры, вышиванки, рушники, писанки), редкие номерные детали, известные бренды и модели, серебро/золото по весу;
- «золото», «антиквариат» и т. п. за копейки или бесплатно — почти всегда подделка или приманка: так и пиши в why и снижай оценку;
- если не можешь опознать модель/клеймо — цена «на глаз», ставь price_conf "низкая".

"""

PROMPT_AD = """
=== Объявление №{n} ===
Сайт: {source}
Цена: {price}
Заголовок: {title}
Место: {location}
Доставка: {delivery}
Описание: {description}
Фото объявления №{n}:"""

PROMPT_TAIL = """

Ответь строго JSON-массивом — по одному объекту на каждое объявление, в том же порядке:
[{{
  "n": номер объявления,
  "score": целое 0-10 (10 = точно стоит забирать ради Etsy/eBay, 0 = мусор),
  "items": "что именно ценного видно (по-русски, коротко)",
  "why": "почему это продаётся, 1 предложение (по-русски)",
  "price_etsy": "реалистичная цена продажи на Etsy в USD, например '$25–35' (или '—', если не для Etsy)",
  "price_ebay": "реалистичная цена продажи на eBay в EUR, например '€20–30' (или '—', если не для eBay)",
  "etsy_ok": true или false — можно ли продавать на Etsy по ИХ правилам: только винтаж (вещи 20+ лет), handmade или материалы для рукоделия; современные фабричные вещи — false,
  "era": "примерный возраст/эпоха вещи по-русски, например '1970-е, СССР' или 'современная'",
  "price_conf": "уверенность в цене: 'высокая' (знаю модель и цены продаж), 'средняя' или 'низкая' (догадка)",
  "query_en": "короткий поисковый запрос НА АНГЛИЙСКОМ для поиска проданных аналогов на eBay: бренд, модель, тип, эпоха — 2–6 слов (например 'Rotring Isograph 0.20 pen', 'Soviet porcelain figurine LFZ')",
  "pickup": "легко ли получить: 'отправит Новой почтой/OLX Доставкой' / 'только самовывоз' / 'неизвестно'"
}}]
Всего объявлений: {count}."""

_last_call = 0.0
calls = 0  # Gemini requests that count against the daily quota (answered ones; 5xx / 429 / network errors don't)
_dead_models: set[str] = set()  # models out of quota / unavailable in this run
fallback_used: set[str] = set()  # fallback models that answered for an overloaded main model (for the run log)
failed_calls = 0  # overloaded (503), rate-limited (429) or network errors — retried, not counted
_lock = threading.Lock()


class QuotaExhausted(RuntimeError):
    pass


def _count(ok: bool) -> None:
    global calls, failed_calls
    with _lock:
        if ok:
            calls += 1
        else:
            failed_calls += 1


def _wait_turn(min_interval: float) -> None:
    """Rate limiter shared by worker threads: request *starts* are spaced min_interval apart,
    so several requests can be in flight while staying under the per-minute limit."""
    global _last_call
    with _lock:
        slot = max(time.time(), _last_call + min_interval)
        _last_call = slot
    delay = slot - time.time()
    if delay > 0:
        time.sleep(delay)


def _image_part(url: str) -> dict | None:
    try:
        resp = get(url, pause=0.1, retries=1)
    except Exception as exc:
        log.debug("image download failed %s: %s", url, exc)
        return None
    mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    if not mime.startswith("image/") or len(resp.content) > 4_000_000:
        return None
    return {"inline_data": {"mime_type": mime, "data": base64.b64encode(resp.content).decode()}}


def _clean(data: dict) -> dict:
    data["score"] = int(data.get("score", 0))
    pe, pb = str(data.get("price_etsy") or "—"), str(data.get("price_ebay") or "—")
    data["price_etsy"], data["price_ebay"] = pe, pb
    data["query_en"] = str(data.get("query_en") or "").strip()[:100]
    ok = data.get("etsy_ok")
    data["etsy_ok"] = ok if isinstance(ok, bool) else str(ok).strip().lower() in ("true", "yes", "да", "1")
    data["era"] = str(data.get("era") or "").strip()[:60]
    conf = str(data.get("price_conf") or "").lower()
    data["price_conf"] = "высокая" if "выс" in conf else "низкая" if "низ" in conf else "средняя"
    data["price"] = f"Etsy {pe} · eBay {pb}"
    return data


def analyze_batch(ads: list[Ad], *, model: str | None = None, criteria: str | None = None,
                  examples: str = "", home: str = "", min_interval: float = 4.5,
                  photos_per_ad: int | None = None, fallbacks: list[str] | None = None) -> list[dict | None]:
    """One Gemini request for several ads. Returns a verdict (or None = retry next run) per ad."""
    if not ads:
        return []
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")
    model = model or os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    if criteria is None:
        from .remote import DEFAULT_CRITERIA
        criteria = DEFAULT_CRITERIA
    per_ad = photos_per_ad or (4 if len(ads) == 1 else 3)

    parts: list[dict] = [{"text": PROMPT_HEAD + criteria + (examples or "") + (("\n" + home) if home else "")}]
    for n, ad in enumerate(ads, 1):
        parts.append({"text": PROMPT_AD.format(
            n=n, source=ad.source, title=ad.title, location=ad.location or "?", delivery=ad.delivery or "?",
            price="бесплатно" if not ad.price else f"{ad.price:g} грн (платное, дешёвое)",
            description=((f"ЭТО ЛОТ: {len(ad.members)} отдельных объявлений одного продавца (список ниже, фото — по одному "
                          f"из первых объявлений). Оцени лот ЦЕЛИКОМ: стоит ли забрать всё сразу и за сколько продать всё вместе.\n"
                          if ad.members else "") + (ad.description or "—"))[:2000])})
        got = 0
        for url in ad.photos[:per_ad]:
            part = _image_part(url)
            if part:
                parts.append(part)
                got += 1
        if not got:
            parts.append({"text": "(фото нет)"})
    parts.append({"text": PROMPT_TAIL.format(count=len(ads))})

    body = {
        "contents": [{"role": "user", "parts": parts}],
        # No temperature/top_p: deprecated for current Gemini models.
        "generationConfig": {"responseMimeType": "application/json"},
    }

    # main model first, then the fallbacks: when the main one is overloaded (503) or out of its own daily quota,
    # the same request goes straight to the next model instead of waiting for the next run
    chain = [m for m in dict.fromkeys([model] + list(fallbacks if fallbacks is not None else DEFAULT_FALLBACKS)) if m]
    resp = None
    for attempt in range(len(chain) + 3):
        alive = [m for m in chain if m not in _dead_models]
        if not alive:
            raise QuotaExhausted("Gemini: all models exhausted or unavailable")
        cur = alive[min(attempt, len(alive) - 1)] if attempt < len(alive) else alive[-1]
        _wait_turn(min_interval)
        try:
            resp = requests.post(API_URL.format(model=cur), params={"key": api_key}, json=body, timeout=90)
        except requests.RequestException as exc:
            _count(False)
            log.warning("Gemini %s request failed (%s), attempt %s", cur, type(exc).__name__, attempt + 1)
            time.sleep(5)
            continue
        _count(resp.status_code < 500 and resp.status_code != 429)
        if resp.status_code == 429:
            if "PerDay" in resp.text or "per day" in resp.text.lower():
                log.warning("Gemini %s: daily quota exhausted%s", cur, " — trying fallback model" if len(alive) > 1 else "")
                _dead_models.add(cur)
                continue
            log.warning("Gemini %s 429 (per-minute limit), waiting %ss", cur, 20 * (attempt + 1))
            time.sleep(20 * (attempt + 1))
            continue
        if resp.status_code >= 500:
            nxt = alive[alive.index(cur) + 1] if alive.index(cur) + 1 < len(alive) else None
            if nxt:
                log.warning("Gemini %s %s (overloaded) → fallback model %s", cur, resp.status_code, nxt)
                fallback_used.add(nxt)
            else:
                log.warning("Gemini %s %s, retrying", cur, resp.status_code)
                time.sleep(8)
            continue
        if resp.status_code != 200:
            log.error("Gemini %s error %s: %s", cur, resp.status_code, resp.text[:500])
            if resp.status_code in (400, 403, 404):
                if cur != model:  # a fallback that doesn't exist / isn't allowed: drop it, don't fail the run
                    _dead_models.add(cur)
                    continue
                raise RuntimeError(f"Gemini request rejected ({resp.status_code}); check model/key")
            return [None] * len(ads)
        try:
            parts_out = resp.json()["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts_out if not p.get("thought"))
            text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            data = json.loads(text)
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            log.warning("Gemini answer not parsed for %s: %s", ", ".join(a.key for a in ads), exc)
            return [None] * len(ads)
        if isinstance(data, dict):
            data = [data]
        out: list[dict | None] = [None] * len(ads)
        for i, item in enumerate(data if isinstance(data, list) else []):
            if not isinstance(item, dict):
                continue
            try:
                n = int(item.get("n", i + 1))
            except (TypeError, ValueError):
                n = i + 1
            if 1 <= n <= len(ads) and out[n - 1] is None:
                try:
                    out[n - 1] = _clean(item)
                except (TypeError, ValueError):
                    pass
        missing = sum(1 for x in out if x is None)
        if missing:
            log.warning("Gemini skipped %s of %s ads in a batch; they retry next run", missing, len(ads))
        return out
    return [None] * len(ads)


def analyze(ad: Ad, **kw) -> dict | None:
    return analyze_batch([ad], **kw)[0]
