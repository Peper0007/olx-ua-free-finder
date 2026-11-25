"""Pickup area: is the ad near home, or will the seller ship it (Nova Poshta / Ukrposhta / OLX Delivery)?"""
from __future__ import annotations

import re

from .common import Ad

_SHIPS = re.compile(r"нов(а|ою|ій|ой) пошт|новапошт|nova ?poshta|укрпошт|ukrposhta|olx доставк|meest|міст експрес|"
                    r"відправл|відправ(лю|имо)|отправл|отправ(лю|им)|пересил|доставк|наложен|накладен", re.I)
_ONLY_PICKUP = re.compile(r"тільки самовивіз|лише самовивіз|только самовывоз|без відправ|не відправляю|не відправл|"
                          r"без отправ|не отправляю|не высылаю", re.I)


def _norm(s: str) -> str:
    return (s or "").lower().replace("м.", " ").replace("г.", " ").replace("смт", " ")


def is_near(location: str, home: dict) -> bool:
    loc = _norm(location)
    places = [home.get("city") or ""] + list(home.get("nearby") or [])
    return any(p and p.lower() in loc for p in places)


def ships(ad: Ad) -> bool:
    text = f"{ad.title} {ad.description}"
    if _ONLY_PICKUP.search(text):
        return False
    # OLX: «OLX Доставка» flag or the «who pays delivery» field means the seller ships
    return bool(ad.delivery) or bool(_SHIPS.search(text))


def home_hint(home: dict) -> str:
    city = home.get("city") or ""
    if not city:
        return ("Владелец забирает вещи по всей Украине посылкой (Нова пошта / Укрпошта / OLX Доставка). "
                "Если продавец явно не отправляет («только самовывоз») и вещь большая или хрупкая, снижай оценку на 1–2 балла.")
    return (f"Владелец живёт в городе {city} и может забрать вещь лично только там и рядом "
            f"({', '.join(home.get('nearby') or [])}); из других городов — только посылкой. Если вещь далеко и продавец "
            "не отправляет, снижай оценку на 1–2 балла.")
