"""Cheap keyword pre-filter: drops obvious junk before spending AI quota.

Edit SKIP_WORDS freely. Matching is case-insensitive on the title only
(descriptions often mention unrelated words).
"""
from __future__ import annotations

import re

from .common import Ad

# Animals, food, building materials, bulky furniture, kids' things, etc. (Ukrainian + Russian)
SKIP_WORDS = [
    # animals
    r"кош[еа]н", r"котен", r"котик", r"кот[иі]\b", r"\bкіт\b", r"\bкот\b", r"кішк", r"кошк", r"цуцен", r"щен",
    r"собак", r"собач", r"\bпес\b", r"\bпс[иа]\b", r"шука\w* (\w+ )?д[іо]м", r"ищ\w* (\w+ )?дом", r"в добр[іы]\w* рук",
    r"папуг", r"попуга", r"хом.?як", r"кролик", r"кріл", r"рибк", r"черепах", r"\bкур[ии]\b", r"курчат", r"адопц", r"притул",
    # food, plants, soil, firewood
    r"дрова", r"\bземл[яю]\b", r"розсад", r"рассад", r"рослин", r"растени", r"кімнатн\w* квіт", r"фікус", r"фикус",
    r"їжа", r"продукт", r"\bкорм", r"консерв",
    # building / bulky stuff / appliances
    r"цегл", r"кирпич", r"плитк", r"шифер", r"вікн", r"\bокн[аоы]", r"двер", r"бойлер", r"радіатор", r"радиатор",
    r"матрац", r"матрас", r"диван", r"ліжк", r"\bкроват", r"шаф[аиу]", r"шкаф", r"стінк", r"стенк", r"кухн",
    r"холодильник", r"пральн", r"стиральн", r"посудомий", r"посудомо", r"плит[аиу]\b", r"духовк", r"шин[аи]\b", r"\bдиск[иі]\b",
    # kids, clothes, school, medicine
    r"дитяч", r"детск", r"для дівчинки", r"для хлопчика", r"для девочки", r"для мальчика", r"на дівчинку", r"на хлопчика",
    r"\bріст\b", r"\bрост\b", r"\bрок[иі]в\b", r"\bроки\b", r"візок", r"коляск", r"підручник", r"учебник", r"ліки", r"лекарств",
    r"памперс", r"підгуз", r"подгуз",
    # services / jobs / non-items / requests
    r"житло", r"жилье", r"квартир", r"кімнат", r"послуг", r"услуг", r"робот[аи]\b", r"работ[аы]\b", r"вивез", r"вывез",
    r"прийму", r"приму в дар", r"візьму", r"возьму", r"потрібн", r"нужн[аоы]", r"шукаю", r"ищу",
]
_SKIP_RE = re.compile("|".join(SKIP_WORDS), re.I)

# Words that hint at collectible / vintage value: such titles always go to the AI,
# even if they also contain a "junk" word ("Зайчета от порцелан", "Ретро шкаф").
KEEP_WORDS = [
    r"порцел", r"фарфор", r"фігур", r"фигур", r"статует", r"кераміч", r"керамик", r"бронз", r"кришта", r"хрустал",
    r"латун", r"\bмідн", r"медн", r"ретро", r"вінтаж", r"винтаж", r"vintage", r"retro", r"антик", r"старовин", r"старин",
    r"\bстар(ий|а|е|і|ый|ая|ое|ые)\b", r"срср", r"ссср", r"радянськ", r"советск", r"колекц", r"коллекц", r"платів", r"пластин",
    r"значк", r"монет", r"марк[иа]\b", r"листівк", r"открытк", r"годинник", r"часы", r"фотоапарат", r"фотоаппарат", r"вініл",
]
_KEEP_RE = re.compile("|".join(KEEP_WORDS), re.I)

# Whole OLX.ua subcategories to skip, written as "cat:<id>" (checked against the ad's category id and URL).
# 4005 = собаки, 4006 = коти (≈70% of the free section), 69/70/71/540/541/542/544/545 = kids' clothes, shoes, furniture.
SKIP_URL_PARTS = ["cat:4005", "cat:4006", "cat:69", "cat:70", "cat:71", "cat:540", "cat:541", "cat:542", "cat:544", "cat:545"]
_SKIP_URL = list(SKIP_URL_PARTS)

# «Охота»: the owner's own words (brands, models, makers). Such ads are never dropped as junk,
# go to the AI first and always reach Telegram with sound, whatever the score. Empty by default.
_HUNT: list[tuple[str, re.Pattern]] = []


def _compile(words: list[str]) -> re.Pattern | None:
    good = []
    for w in words or []:
        w = (w or "").strip()
        if not w:
            continue
        try:
            re.compile(w)
            good.append(w)
        except re.error:
            good.append(re.escape(w))  # treat broken regex as plain text
    return re.compile("|".join(good), re.I) if good else None


def configure(skip_words: list[str] | None = None, keep_words: list[str] | None = None,
              skip_url_parts: list[str] | None = None, hunt_words: list[str] | None = None) -> None:
    """Replace the built-in lists (used when the dashboard provides its own)."""
    global _SKIP_RE, _KEEP_RE, _SKIP_URL, _HUNT
    if hunt_words is not None:
        _HUNT = []
        for w in hunt_words:
            w = (w or "").strip()
            if w:
                rx = _compile([w])
                if rx is not None:
                    _HUNT.append((w, rx))
    if skip_url_parts is not None:
        _SKIP_URL = [p.strip() for p in skip_url_parts if p and p.strip()]
    if skip_words is not None:
        _SKIP_RE = _compile(skip_words)
    if keep_words is not None:
        _KEEP_RE = _compile(keep_words)


def hunt_match(ad: Ad) -> str:
    """The first «охота» word found in the title or description ('' = none)."""
    text = f"{ad.title or ''} {ad.description or ''}"
    for word, rx in _HUNT:
        if rx.search(text):
            return word
    return ""


def is_junk(ad: Ad) -> bool:
    if hunt_match(ad):
        return False
    url = (ad.url or "") + (f" cat:{ad.category_id} " if ad.category_id else "")
    if any(part and (part in url if not part.startswith("cat:") else f" {part} " in url) for part in _SKIP_URL):
        return True
    title = ad.title or ""
    if _SKIP_RE is None or not _SKIP_RE.search(title):
        return False
    return not (_KEEP_RE is not None and _KEEP_RE.search(title))
