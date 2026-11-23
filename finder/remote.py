"""Firestore link for the dashboard.

- config/settings : settings edited in the dashboard (fallback: built-in defaults)
- finds/{key}     : every ad the AI scored (+ the user's status from the dashboard)
- runs/{id}       : statistics of each bot run

Works only when FIREBASE_SERVICE_ACCOUNT (service-account JSON) is set;
otherwise every function is a harmless no-op and the bot uses defaults.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .common import Ad, log

DEFAULT_CRITERIA = """Найди вещи, которые реально можно перепродать на Etsy/eBay за деньги, оправдывающие доставку из Украины:
винтаж и антиквариат (20+ лет), соц/советские предметы, фарфор, хрусталь, керамика, бижутерия, часы,
янтарь/бакелит, значки, монеты, марки, открытки, старые фото, книги/журналы/мануалы, плакаты, эмалированные
таблички, ретро-техника (фотоаппараты, радио, ресиверы, винил, кассеты, CD/DVD коллекционные), игрушки,
предметы декора, брендовые или коллекционные вещи, необычные инструменты, иконы, текстиль ручной работы и т.п.

НЕ интересно: животные, обычная мебель и техника, обычная одежда и обувь, детские вещи, стройматериалы,
еда, растения, массовые дешёвые вещи, громоздкие предметы без коллекционной ценности.

Забирать будут по всей Украине, в основном посылкой: вещи, которые продавец готов отправить
(OLX Доставка / Нова пошта / Укрпошта), а также небольшие и лёгкие — ценнее. Огромное и хрупкое «только самовывоз» — ниже."""

DEFAULTS: dict = {
    "paused": False,
    "telegram_enabled": True,
    "min_score": 6,
    "max_ai_per_run": 100,
    "learn_from_marks": True,   # feed the owner's dashboard marks to the AI as examples
    "loud_score": 8,            # Telegram: score >= this arrives with sound, lower ones silently
    "model": "gemini-3.5-flash-lite",
    "criteria": DEFAULT_CRITERIA,
    "sources": {
        "olx": {"enabled": True, "pages": 25},
        "bazar": {"enabled": False, "pages": 0},
        "alo": {"enabled": False, "queries": []},
    },
    "skip_words": None,  # None → built-in list from prefilter.py
    "keep_words": None,
    "skip_url_parts": None,  # None → built-in list (OLX animals/kids categories)
}

_db = None


def _client():
    global _db
    if _db is not None:
        return _db or None
    raw = os.environ.get("FIREBASE_SERVICE_ACCOUNT")
    if not raw:
        _db = False
        return None
    try:
        import firebase_admin
        from firebase_admin import credentials, firestore

        cred = credentials.Certificate(json.loads(raw))
        app = firebase_admin.initialize_app(cred)
        _db = firestore.client(app)
        return _db
    except Exception as exc:
        log.error("Firestore init failed: %s", exc)
        _db = False
        return None


def enabled() -> bool:
    return _client() is not None


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is not None:
            out[k] = v
    return out


def load_settings() -> dict:
    """Defaults ← env vars ← Firestore config/settings (dashboard wins)."""
    s = json.loads(json.dumps(DEFAULTS))
    env_map = {"MIN_SCORE": "min_score", "MAX_AI_PER_RUN": "max_ai_per_run", "GEMINI_MODEL": "model"}
    for env, key in env_map.items():
        val = os.environ.get(env)
        if val:
            s[key] = int(val) if key != "model" else val
    for env, src in (("OLX_PAGES", "olx"), ("BAZAR_PAGES", "bazar")):
        if os.environ.get(env, "").isdigit():
            s["sources"][src]["pages"] = int(os.environ[env])

    db = _client()
    if db is not None:
        try:
            ref = db.collection("config").document("settings")
            snap = ref.get()
            if snap.exists:
                s = _merge(s, snap.to_dict())
            else:
                # First run with Firestore: publish defaults so the dashboard shows them.
                from .prefilter import KEEP_WORDS, SKIP_URL_PARTS, SKIP_WORDS

                seed = json.loads(json.dumps(s))
                seed["skip_words"] = SKIP_WORDS
                seed["keep_words"] = KEEP_WORDS
                seed["skip_url_parts"] = SKIP_URL_PARTS
                ref.set(seed)
                s = seed
        except Exception as exc:
            log.error("Could not load settings from Firestore: %s", exc)
    if s.get("skip_url_parts") is None:
        from .prefilter import SKIP_URL_PARTS
        s["skip_url_parts"] = SKIP_URL_PARTS
    return s


def _now():
    return datetime.now(timezone.utc)


def start_run(trigger: str) -> str | None:
    db = _client()
    if db is None:
        return None
    try:
        ref = db.collection("runs").document()
        ref.set({"started_at": _now(), "trigger": trigger, "status": "running"})
        return ref.id
    except Exception as exc:
        log.error("Firestore start_run failed: %s", exc)
        return None


def finish_run(run_id: str | None, stats: dict) -> None:
    db = _client()
    if db is None or not run_id:
        return
    try:
        db.collection("runs").document(run_id).set({**stats, "finished_at": _now()}, merge=True)
    except Exception as exc:
        log.error("Firestore finish_run failed: %s", exc)


def load_feedback_examples(max_each: int = 12) -> str:
    """Builds a prompt block from the owner's own marks in the dashboard (taken / interesting vs skip),
    so the AI calibrates to what this particular reseller actually wants."""
    db = _client()
    if db is None:
        return ""
    try:
        docs = [d.to_dict() for d in db.collection("finds")
                .where("status", "in", ["taken", "interesting", "gone", "skip"]).limit(300).stream()]
    except Exception as exc:
        log.warning("Firestore feedback load failed: %s", exc)
        return ""

    def when(d):
        t = d.get("status_at") or d.get("analyzed_at")
        try:
            return t.timestamp()
        except Exception:
            return 0

    docs.sort(key=when, reverse=True)
    good = [d for d in docs if d.get("status") in ("taken", "interesting", "gone")][:max_each]
    bad = [d for d in docs if d.get("status") == "skip"][:max_each]
    if not good and not bad:
        return ""

    def line(d):
        what = (d.get("items") or "").strip()
        return f"- {d.get('title', '')[:90]}" + (f" ({what[:90]})" if what else "") + f" — ИИ дал {d.get('score', '?')}/10"

    out = ["\n\nОПЫТ ВЛАДЕЛЬЦА — его собственные отметки в дашборде. Подстрой оценки под его вкус:"]
    if good:
        out.append("Это он ЗАБРАЛ, отметил «интересно» или не успел забрать (похожему ставь оценку выше):")
        out += [line(d) for d in good]
    if bad:
        out.append("Это он отметил «мимо» (похожему ставь оценку ниже, даже если вещь в целом продаётся):")
        out += [line(d) for d in bad]
    return "\n".join(out) + "\n"


_last_progress = 0.0


def progress(run_id: str | None, force: bool = False, **fields) -> None:
    """Live status for the dashboard. Throttled to one write per ~4 s to stay well inside free Firestore quotas."""
    global _last_progress
    db = _client()
    if db is None or not run_id:
        return
    import time as _t
    if not force and _t.time() - _last_progress < 4:
        return
    _last_progress = _t.time()
    try:
        db.collection("runs").document(run_id).set({"progress": {**fields, "at": _now()}}, merge=True)
    except Exception as exc:
        log.warning("Firestore progress failed: %s", exc)


def fingerprint(ad: Ad) -> str:
    from .dedup import fingerprint as fp
    return fp(ad)


def save_find(ad: Ad, verdict: dict, sent: bool, run_id: str | None) -> None:
    db = _client()
    if db is None:
        return
    doc_id = ad.key.replace(":", "_")
    try:
        db.collection("finds").document(doc_id).set(
            {
                "key": ad.key,
                "source": ad.source,
                "title": ad.title,
                "url": ad.url,
                "location": ad.location,
                "posted": ad.posted,
                "delivery": ad.delivery,
                "photos": ad.photos[:4],
                "description": (ad.description or "")[:600],
                "score": int(verdict.get("score", 0)),
                "items": str(verdict.get("items", ""))[:500],
                "why": str(verdict.get("why", ""))[:500],
                "price": str(verdict.get("price", ""))[:100],
                "price_etsy": str(verdict.get("price_etsy", ""))[:60],
                "price_ebay": str(verdict.get("price_ebay", ""))[:60],
                "query_en": str(verdict.get("query_en", ""))[:100],
                "etsy_ok": bool(verdict.get("etsy_ok", False)),
                "era": str(verdict.get("era", ""))[:60],
                "posted_at": ad.posted_at or None,
                "fingerprint": fingerprint(ad),
                "pickup": str(verdict.get("pickup", ""))[:100],
                "sent": sent,
                "analyzed_at": _now(),
                "run_id": run_id,
            },
            merge=True,  # keeps the user's "status" field if the ad is re-analyzed
        )
    except Exception as exc:
        log.error("Firestore save_find failed: %s", exc)
