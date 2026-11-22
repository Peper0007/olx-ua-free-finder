"""Minimal Telegram Bot API client."""
from __future__ import annotations

import html
import re
import os
import time

import requests

from .common import Ad, log

API = "https://api.telegram.org/bot{token}/{method}"
SOURCE_NAMES = {"olx": "OLX.ua"}


def _call(method: str, **payload) -> dict:
    token = os.environ["TELEGRAM_TOKEN"]
    resp = requests.post(API.format(token=token, method=method), json=payload, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data


def send_text(text: str, silent: bool = False) -> None:
    _call(
        "sendMessage",
        chat_id=os.environ["TELEGRAM_CHAT_ID"],
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
        disable_notification=silent,
    )


def price_links(query: str) -> str:
    """Links to check real prices: eBay sold listings (DE and US) and Etsy search."""
    from urllib.parse import quote_plus
    q = quote_plus(query)
    return (f'<a href="https://www.ebay.de/sch/i.html?_nkw={q}&LH_Sold=1&LH_Complete=1">eBay.de продано</a> · '
            f'<a href="https://www.ebay.com/sch/i.html?_nkw={q}&LH_Sold=1&LH_Complete=1">eBay.com продано</a> · '
            f'<a href="https://www.etsy.com/search?q={q}">Etsy</a>')


def age_text(ad: Ad) -> str:
    if ad.posted_at:
        mins = max(0, int((time.time() - ad.posted_at) / 60))
        if mins < 1:
            return "только что"
        if mins < 60:
            return f"{mins} мин назад"
        if mins < 48 * 60:
            return f"{mins // 60} ч {mins % 60} мин назад" if mins < 6 * 60 else f"{mins // 60} ч назад"
        return f"{mins // 1440} дн назад"
    return ad.posted or ""


def seller_message(ad: Ad) -> str:
    return (f"Добрий день! Ще актуально „{ad.title[:80]}“? "
            "Можу забрати Новою поштою — доставку оплачу сам. Дякую!")


def format_ad(ad: Ad, verdict: dict | None) -> str:
    e = html.escape
    lines = [f"<b>{e(ad.title)}</b>"]
    if verdict:
        age = age_text(ad)
        lines.append(f"⭐ {verdict.get('score', '?')}/10" + (f" · 🕐 опубликовано {e(age)}" if age else ""))
        if "etsy_ok" in verdict:
            era = f" ({e(str(verdict['era']))})" if verdict.get("era") else ""
            lines.append(("🏷 Etsy ✅ винтаж/handmade" if verdict["etsy_ok"] else "🏷 только eBay — для Etsy не подходит") + era)
        if verdict.get("price_etsy") or verdict.get("price_ebay"):
            lines.append(f"💰 Etsy: {e(str(verdict.get('price_etsy', '—')))} · eBay: {e(str(verdict.get('price_ebay', '—')))}")
        elif verdict.get("price"):
            lines.append(f"💰 {e(str(verdict['price']))}")
        if verdict.get("items"):
            lines.append(f"🔎 {e(str(verdict['items']))}")
        if verdict.get("why"):
            lines.append(f"💡 {e(str(verdict['why']))}")
        if verdict.get("pickup"):
            lines.append(f"🚚 {e(str(verdict['pickup']))}")
        if verdict.get("query_en"):
            lines.append(f"🔍 Проверить цену: {price_links(str(verdict['query_en']))}")
    meta = " · ".join(x for x in (ad.location, ad.posted, f"доставка: {ad.delivery}" if ad.delivery else "") if x)
    if meta:
        lines.append(f"📍 {e(meta)}")
    lines.append(f'<a href="{e(ad.url)}">Открыть на {SOURCE_NAMES.get(ad.source, ad.source)}</a>')
    if verdict:
        lines.append(f"✉️ Продавцу (нажми, чтобы скопировать):\n<code>{e(seller_message(ad))}</code>")
    return "\n".join(lines)


def send_ad(ad: Ad, verdict: dict | None, silent: bool = False) -> None:
    caption = format_ad(ad, verdict)
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    visible = html.unescape(re.sub(r"<[^>]+>", "", caption))  # Telegram's 1024 limit counts text, not markup
    if ad.photos and len(visible) <= 1024:
        try:
            _call("sendPhoto", chat_id=chat_id, photo=ad.photos[0], caption=caption, parse_mode="HTML",
                  disable_notification=silent)
            return
        except Exception as exc:  # e.g. Telegram can't fetch the image URL
            log.warning("sendPhoto failed (%s), falling back to text", exc)
    send_text(caption, silent=silent)


def print_chat_ids() -> None:
    """Helper: prints chat ids of everyone who wrote to the bot recently."""
    data = _call("getUpdates")
    found = {}
    for upd in data.get("result", []):
        msg = (upd.get("message") or upd.get("edited_message") or upd.get("channel_post")
               or upd.get("edited_channel_post") or (upd.get("my_chat_member") or {}))
        chat = msg.get("chat") or {}
        if chat.get("id"):
            kind = "канал" if chat.get("type") == "channel" else chat.get("type", "")
            found[chat["id"]] = f'{chat.get("title") or chat.get("first_name") or ""} [{kind}]'
    if not found:
        print("Нет сообщений. Напиши боту (или опубликуй пост в канале, где бот — админ) и запусти ещё раз.")
    for cid, name in found.items():
        # digits spelled as letters too (0→a … 9→j): GitHub log masking can't hide them
        spelled = str(cid).translate(str.maketrans("0123456789", "abcdefghij"))
        print(f"TELEGRAM_CHAT_ID = {cid}   ({name})   [{spelled}]")
