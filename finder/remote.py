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
    "paid_min_score": 6,        # Telegram threshold for cheap paid ads (they score high often; avoid flooding the channel)
    "max_ai_per_run": 100,
    "learn_from_marks": True,   # feed the owner's dashboard marks to the AI as examples
    "loud_score": 6,            # Telegram: score >= this arrives with sound, lower ones silently
    "model": "gemini-3.5-flash-lite",
    "fallback_models": ["gemini-3.1-flash-lite", "gemini-flash-latest"],  # tried when the main one is overloaded
    "criteria": DEFAULT_CRITERIA,
    "ai_batch": 4,              # ads per Gemini request (the daily quota counts requests, not ads)
    "gemini_rpd": 500,          # Gemini daily request limit; the bot spreads it over the day's runs
    "check_alive": True,        # re-check open finds and move removed ads to «Забранные другими»
    "home": {                   # pickup area: empty city = whole Ukraine by parcel (Nova Poshta / Ukrposhta / OLX Delivery)
        "city": "",
        "nearby": [],
        "only_near": False,     # True = skip ads that are neither near home nor shippable
    },
    "night": {"enabled": True, "from": 1, "to": 6},  # no scheduled runs between these local hours
    "digest": {"enabled": True, "hour": 21},  # evening summary in Telegram (Kyiv time)
    "sources": {
        "olx": {"enabled": True, "pages": 25},
        "olx_cheap": {"enabled": True, "max_uah": 200, "pages": 3,
                      "categories": ["hobbi-otdyh-i-sport/antikvariat-kollektsii"]},
    },
    "uah_per_eur": 48,     # only for the profit table in the dashboard (purchase price of paid ads in EUR)
    "hunt_words": [],      # «охота»: always to Telegram with sound (dashboard → «Фильтры слов»)
    "skip_words": None,  # None → built-in list from prefilter.py
    "keep_words": None,
    "skip_url_parts": None,  # None → built-in list (OLX animals/kids categories)
}

UAH_PER_EUR = 48.0  # overwritten from settings in load_settings()
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
    global UAH_PER_EUR
    s = json.loads(json.dumps(DEFAULTS))
    env_map = {"MIN_SCORE": "min_score", "MAX_AI_PER_RUN": "max_ai_per_run", "GEMINI_MODEL": "model"}
    for env, key in env_map.items():
        val = os.environ.get(env)
        if val:
            s[key] = int(val) if key != "model" else val
    for env, src in (("OLX_PAGES", "olx"),):
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
    try:
        UAH_PER_EUR = max(1.0, float(s.get("uah_per_eur") or 48))
    except (TypeError, ValueError):
        pass
    return s


def _now():
    return datetime.now(timezone.utc)


def start_run(trigger: str) -> str | None:
    db = _client()
    if db is None:
        return None
    # A run killed by GitHub's timeout never reports back: close such leftovers so the dashboard
    # doesn't show "идёт проверка" forever.
    try:
        cutoff = datetime.now(timezone.utc).timestamp() - 40 * 60
        for d in db.collection("runs").where("status", "==", "running").stream():
            started = d.to_dict().get("started_at")
            if started is not None and started.timestamp() < cutoff:
                d.reference.set({"status": "failed", "finished_at": _now(),
                                 "errors": ["Прервана: GitHub остановил запуск по таймауту (30 мин)"]}, merge=True)
    except Exception as exc:
        log.warning("Firestore stale-run cleanup failed: %s", exc)
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
    good = [d for d in docs if d.get("status") in ("taken", "interesting", "gone") and not d.get("auto_gone")][:max_each]
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


# Statuses set in the dashboard after which the item must never be sent again (even if reposted).
BLOCK_STATUSES = ["spam", "gone", "taken"]


def load_blocked() -> tuple[set[str], set[str], set[str]]:
    """Keys, fingerprints and (for spam) title-only fingerprints of finds the owner marked
    «спам», «уже забрали» or «забрал» in the dashboard."""
    keys: set[str] = set(); fps: set[str] = set(); spam_titles: set[str] = set()
    db = _client()
    if db is None:
        return keys, fps, spam_titles
    from finder.dedup import title_fingerprint
    try:
        for d in db.collection("finds").where("status", "in", BLOCK_STATUSES).stream():
            f = d.to_dict()
            if f.get("key"):
                keys.add(f["key"])
            fake = Ad(f.get("source", ""), "", f.get("title", ""), "", location=f.get("location", ""))
            fps.add(f.get("fingerprint") or fingerprint(fake))
            fps.add(fingerprint(fake))
            if f.get("status") == "spam":
                tfp = title_fingerprint(f.get("title", ""))
                if tfp:
                    spam_titles.add(tfp)
    except Exception as exc:
        log.warning("Firestore blocked-list load failed: %s", exc)
    return keys, fps, spam_titles


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


def _tg_ref(doc_id: str) -> str:
    from .tgbuttons import ref
    return ref(doc_id)


def already_sent(ad: Ad) -> bool:
    """True if this ad already went to Telegram (e.g. from an overlapping run)."""
    db = _client()
    if db is None:
        return False
    try:
        snap = db.collection("finds").document(ad.key.replace(":", "_")).get()
        return bool(snap.exists and (snap.to_dict() or {}).get("sent"))
    except Exception:
        return False


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
                "price_conf": str(verdict.get("price_conf", ""))[:20],
                "query_en": str(verdict.get("query_en", ""))[:100],
                "etsy_ok": bool(verdict.get("etsy_ok", False)),
                "era": str(verdict.get("era", ""))[:60],
                "posted_at": ad.posted_at or None,
                "price_uah": float(ad.price or 0),
                "price_eur": round(float(ad.price or 0) / UAH_PER_EUR, 2),
                "fingerprint": fingerprint(ad),
                "pickup": str(verdict.get("pickup", ""))[:100],
                "hunt": str(verdict.get("hunt", ""))[:60],
                "tg_ref": _tg_ref(doc_id),
                "lot": [{"title": m.title[:120], "url": m.url, "price": float(m.price or 0)} for m in ad.members[:30]],
                "sent": sent,
                "analyzed_at": _now(),
                "run_id": run_id,
            },
            merge=True,  # keeps the user's "status" field if the ad is re-analyzed
        )
    except Exception as exc:
        log.error("Firestore save_find failed: %s", exc)


# ---------- quota, liveness, digest ----------
def _pacific_midnight_utc() -> datetime:
    """Gemini daily quotas reset at midnight Pacific time."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/Los_Angeles"))
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def gemini_used_today(exclude_run: str | None = None) -> int | None:
    """Gemini requests spent since the last quota reset, from the runs log (None = unknown)."""
    db = _client()
    if db is None:
        return None
    try:
        used = 0
        for d in db.collection("runs").where("started_at", ">=", _pacific_midnight_utc()).stream():
            if d.id == exclude_run:
                continue
            r = d.to_dict()
            if r.get("status") == "running":
                used += int((r.get("progress") or {}).get("calls") or 0)
            else:
                used += int(r.get("ai_calls") or r.get("analyzed") or 0)
        return used
    except Exception as exc:
        log.warning("Firestore quota read failed: %s", exc)
        return None


def load_open_finds(min_score: int, max_n: int = 60, recheck_hours: float = 2.0) -> list[dict]:
    """Open finds (new / interesting, at/above the threshold, last 30 days) due for an «is it still online?» check."""
    db = _client()
    if db is None:
        return []
    now = _now().timestamp()
    out = []
    try:
        for d in db.collection("finds").where("score", ">=", min_score).stream():
            f = d.to_dict()
            if (f.get("status") or "new") not in ("new", "interesting"):
                continue
            t = f.get("analyzed_at")
            if t is None or now - t.timestamp() > 30 * 86400:
                continue
            c = f.get("checked_at")
            if c is not None and now - c.timestamp() < recheck_hours * 3600:
                continue
            out.append({"id": d.id, "url": f.get("url", ""), "source": f.get("source", ""),
                        "checked": c.timestamp() if c else 0})
    except Exception as exc:
        log.warning("Firestore open-finds load failed: %s", exc)
        return []
    out.sort(key=lambda x: x["checked"])  # never-checked first
    return out[:max_n]


def mark_checked(doc_id: str, alive: bool) -> None:
    db = _client()
    if db is None:
        return
    patch = {"checked_at": _now()}
    if not alive:
        patch.update(status="gone", status_at=_now(), auto_gone=True)
    try:
        db.collection("finds").document(doc_id).set(patch, merge=True)
    except Exception as exc:
        log.warning("Firestore mark_checked failed: %s", exc)


def finds_since(ts: datetime) -> list[dict]:
    db = _client()
    if db is None:
        return []
    try:
        return [d.to_dict() for d in db.collection("finds").where("analyzed_at", ">=", ts).stream()]
    except Exception as exc:
        log.warning("Firestore finds_since failed: %s", exc)
        return []


def runs_since(ts: datetime) -> list[dict]:
    db = _client()
    if db is None:
        return []
    try:
        return [d.to_dict() for d in db.collection("runs").where("started_at", ">=", ts).stream()]
    except Exception as exc:
        log.warning("Firestore runs_since failed: %s", exc)
        return []


def claim_digest(day: str) -> bool:
    """True once per Kyiv day: records that the evening digest for `day` was sent."""
    db = _client()
    if db is None:
        return False
    ref = db.collection("config").document("state")
    try:
        snap = ref.get()
        if snap.exists and snap.to_dict().get("digest_day") == day:
            return False
        ref.set({"digest_day": day, "digest_at": _now()}, merge=True)
        return True
    except Exception as exc:
        log.warning("Firestore digest claim failed: %s", exc)
        return False
