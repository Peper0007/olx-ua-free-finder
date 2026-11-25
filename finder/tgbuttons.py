"""Status buttons under each find in Telegram («интересно», «забрал», «невыгодно», «мимо», …).

The bot has no server, so a tap is picked up at the next run (every 20 min): getUpdates → the find's status
in Firestore → the keyboard is redrawn with a ✅ on the chosen button.

Several bots (Bulgaria, Belgium, Ukraine) may share one Telegram bot token, so each button carries a project tag and
each bot handles only its own taps. Updates are confirmed to Telegram only once they are older than
CONFIRM_AFTER, by then every bot (they all run every 20 min) has seen them.
"""
from __future__ import annotations

import hashlib
import time

from . import remote
from .common import log

PROJECT = "ua"  # tag inside callback_data; unique per bot sharing the token
CONFIRM_AFTER = 2 * 3600

# code in callback_data → (status in Firestore, button label)
BUTTONS = {
    "i": ("interesting", "⭐ интересно"),
    "t": ("taken", "✅ забрал"),
    "l": ("low_profit", "💸 невыгодно"),
    "k": ("skip", "👎 мимо"),
    "g": ("gone", "🏃 уже забрали"),
    "s": ("spam", "🚫 спам"),
}
ROWS = [["i", "t", "l"], ["k", "g", "s"]]
LABEL = {v[0]: v[1] for v in BUTTONS.values()}


def doc_id(ad_key: str) -> str:
    return ad_key.replace(":", "_")


def ref(doc: str) -> str:
    """Short reference to a find that fits Telegram's 64-byte callback_data."""
    return doc if len(doc.encode()) <= 48 else "h:" + hashlib.sha1(doc.encode()).hexdigest()[:20]


def keyboard(doc: str, chosen: str = "") -> dict:
    r = ref(doc)
    return {"inline_keyboard": [[{"text": ("☑️ " if BUTTONS[c][0] == chosen else "") + BUTTONS[c][1],
                                  "callback_data": f"{PROJECT}|{c}|{r}"} for c in row] for row in ROWS]}


def process(call) -> int:
    """Applies taps since the last run. `call` is telegram._call. Returns how many statuses were changed."""
    db = remote._client()
    if db is None:
        return 0
    try:
        updates = call("getUpdates", timeout=0, limit=100, allowed_updates=["callback_query", "channel_post", "message", "my_chat_member"])["result"]
    except Exception as exc:
        log.warning("Telegram getUpdates failed: %s", exc)
        return 0
    state_ref = db.collection("config").document("telegram")
    try:
        state = (state_ref.get().to_dict() or {})
    except Exception:
        state = {}
    seen: dict = dict(state.get("seen") or {})   # update_id → first time this bot saw it
    done: list = list(state.get("done") or [])   # callback ids already applied
    now = time.time()
    changed = 0
    for upd in updates:
        uid = str(upd.get("update_id"))
        seen.setdefault(uid, now)
        cq = upd.get("callback_query") or {}
        parts = (cq.get("data") or "").split("|", 2)
        if len(parts) != 3 or parts[0] != PROJECT or cq.get("id") in done or parts[1] not in BUTTONS:
            continue
        status = BUTTONS[parts[1]][0]
        r = parts[2]
        try:
            if r.startswith("h:"):
                docs = list(db.collection("finds").where("tg_ref", "==", r).limit(1).stream())
                target = docs[0].reference if docs else None
            else:
                target = db.collection("finds").document(r)
            if target is None:
                done.append(cq["id"])
                continue
            from datetime import datetime, timezone
            target.set({"status": status, "status_at": datetime.now(timezone.utc), "status_via": "telegram"}, merge=True)
            changed += 1
            log.info("Telegram button: %s → %s", target.id, status)
            msg = cq.get("message") or {}
            if msg:
                try:
                    call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
                         reply_markup=keyboard(target.id, status))
                except Exception as exc:
                    log.debug("keyboard redraw failed: %s", exc)
            try:
                call("answerCallbackQuery", callback_query_id=cq["id"], text=f"Отмечено: {LABEL.get(status, status)}")
            except Exception:
                pass  # too old to answer — fine, the ☑️ on the button shows it
        except Exception as exc:
            log.warning("Telegram button failed: %s", exc)
            continue
        done.append(cq["id"])
    # confirm (drop) updates every bot has had time to see
    old = [int(u) for u, t in seen.items() if now - float(t) > CONFIRM_AFTER]
    if old:
        try:
            call("getUpdates", offset=max(old) + 1, timeout=0, limit=1)
        except Exception as exc:
            log.debug("getUpdates confirm failed: %s", exc)
        seen = {u: t for u, t in seen.items() if int(u) > max(old)}
    try:
        state_ref.set({"seen": seen, "done": done[-300:]}, merge=False)
    except Exception as exc:
        log.warning("Telegram state save failed: %s", exc)
    if changed:
        log.info("Telegram buttons applied: %s", changed)
    return changed
