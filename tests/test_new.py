"""Offline tests for: cheap OLX ads, early stop, pickup area, batched Gemini, alive check, digest, full run."""
import json
import os
import sys
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from finder import ai, alive, common, geo
from finder.common import Ad, stop_when_seen
from finder.sources import olx


def olx_state(ads):
    state = json.dumps({"listing": {"listing": {"ads": ads}}}, ensure_ascii=False)
    return "<script>window.__PRERENDERED_STATE__= " + json.dumps(state, ensure_ascii=False) + ";\n</script>"


def ad_json(i, price, title="Стара чаша"):
    p = {"free": True} if price == 0 else {"free": False, "regularPrice": {"value": price}} if price else {"free": False, "regularPrice": None}
    return {"id": i, "title": title, "url": f"https://www.olx.bg/d/ad/x-CID1609-ID{i}.html", "price": p,
            "location": {"pathName": "гр. София, Лозенец"}, "photos": [], "createdTime": "2026-11-25T10:00:00+02:00"}


def test_cheap_parse():
    html = olx_state([ad_json(1, 0), ad_json(2, 3.5), ad_json(3, 12), ad_json(4, None)])
    assert [a.ad_id for a in olx.parse_listing(html)] == ["1"]  # free section: only free
    ads = olx.parse_listing(html, max_price=5)
    assert [(a.ad_id, a.price) for a in ads] == [("1", 0.0), ("2", 3.5)]


def test_stop_when_seen():
    pages = [[Ad("olx", str(i * 10 + j), "t", "u") for j in range(3)] for i in range(6)]
    fetched = []

    def gen():
        for p in pages:
            fetched.append(1)
            yield p
    seen = {a.key: 1 for p in pages[1:] for a in p}  # only page 1 has new ads
    out = stop_when_seen(gen(), seen, "t")
    assert len(fetched) == 3 and len(out) == 9, (len(fetched), len(out))  # page1 new, 2-3 quiet → stop
    assert len(stop_when_seen(iter(pages), None, "t")) == 18


def test_geo():
    home = {"city": "Київ", "nearby": ["Бровари"]}
    assert geo.is_near("Київ, Печерський, Київська область", home) and geo.is_near("Бровари, Київська область", home)
    assert not geo.is_near("Одеса, Приморський", home)
    assert geo.ships(Ad("olx", "1", "Ваза", "u", delivery="OLX Доставка"))
    assert geo.ships(Ad("olx", "1", "Ваза", "u", description="Відправлю Новою поштою"))
    assert not geo.ships(Ad("olx", "1", "Ваза", "u", description="Тільки самовивіз"))
    assert "Київ" in geo.home_hint(home) and "по всей Украине" in geo.home_hint({})


class FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text or json.dumps(payload or {})

    def json(self):
        return self._payload


def gemini_answer(items):
    return FakeResp(200, {"candidates": [{"content": {"parts": [{"text": json.dumps(items, ensure_ascii=False)}]}}]})


def test_ai_batch():
    os.environ["GEMINI_API_KEY"] = "x"
    ads = [Ad("olx", str(i), f"Вещь {i}", "u", price=2 if i == 2 else 0) for i in range(1, 4)]
    sent = {}

    def post(url, params, json, timeout):
        sent["body"] = json
        return gemini_answer([{"n": 3, "score": 9, "etsy_ok": "true"}, {"n": 1, "score": 2}])
    with mock.patch.object(ai.requests, "post", post), mock.patch.object(ai, "_image_part", lambda u: None):
        before = ai.calls
        out = ai.analyze_batch(ads, min_interval=0)
    assert ai.calls - before == 1  # one request for three ads
    assert out[0]["score"] == 2 and out[1] is None and out[2]["score"] == 9 and out[2]["etsy_ok"] is True
    text = "".join(p.get("text", "") for p in sent["body"]["contents"][0]["parts"])
    assert "Объявление №3" in text and "2 грн (платное" in text and "Всего объявлений: 3" in text

    def quota(url, params, json, timeout):
        return FakeResp(429, text="Quota exceeded GenerateRequestsPerDay")
    with mock.patch.object(ai.requests, "post", quota):
        try:
            ai.analyze_batch(ads, min_interval=0)
            assert False
        except ai.QuotaExhausted:
            pass
    assert ai.DEFAULT_MODEL in ai._dead_models and set(ai.DEFAULT_FALLBACKS) <= ai._dead_models
    ai._dead_models.clear()


def test_rate_limiter_spacing():
    ai._last_call = 0
    t0 = time.time()
    for _ in range(3):
        ai._wait_turn(0.2)
    assert time.time() - t0 >= 0.39


def test_alive():
    class S:
        def __init__(self, status, text=""):
            self.status_code, self.text = status, text
    cases = {"a": S(410), "b": S(404), "c": S(200, "ok"), "d": S(403),
             "https://www.olx.ua/e": S(200, "i18n: Оголошення неактивне")}  # OLX active pages carry this text too
    with mock.patch.object(alive, "session") as sess:
        sess.return_value.get.side_effect = lambda url, **kw: cases[url]
        res = {it["url"]: a for it, a in alive.check([{"id": k, "url": k} for k in cases], workers=2)}
    assert res == {"a": False, "b": False, "c": True, "d": None, "https://www.olx.ua/e": True}, res


def test_full_run():
    """main.run with fake sites, Gemini, Telegram and Firestore: priority, batching, cheap ads, alive check, digest."""
    import main
    from finder import digest, remote, state, telegram
    ads = [Ad("olx", "1", "Порцелянова ваза", "u1", location="Одеса", posted_at=time.time()),
           Ad("olx", "2", "Старий фотоапарат", "u2", location="Київ, Печерський", posted_at=time.time() - 9000),
           Ad("olx", "3", "Віддам котика", "u3", location="Київ"),
           Ad("olx", "4", "Значки СРСР", "u4", location="Львів", price=3, posted_at=time.time() - 100)]
    settings = json.loads(json.dumps(remote.DEFAULTS))
    settings.update(ai_batch=2, paid_min_score=8, home={"city": "Київ", "nearby": [], "only_near": False}, sources={**settings["sources"], "bazar": {"enabled": False}, "alo": {"enabled": False},
                                         "olx_cheap": {"enabled": False}})
    batches, tg, saved, marked = [], [], [], []

    def fake_batch(group, **kw):
        batches.append([a.ad_id for a in group])
        return [{"score": 8 if a.ad_id == "2" else 3, "price": "x"} for a in group]
    with mock.patch.object(remote, "load_settings", lambda: settings), \
         mock.patch.object(remote, "start_run", lambda t: "r1"), mock.patch.object(remote, "finish_run", lambda *a: saved.append(a[1])), \
         mock.patch.object(remote, "load_blocked", lambda: (set(), set(), set())), \
         mock.patch.object(remote, "load_feedback_examples", lambda: ""), \
         mock.patch.object(remote, "gemini_used_today", lambda exclude_run=None: 0), \
         mock.patch.object(remote, "save_find", lambda *a, **k: None), mock.patch.object(remote, "progress", lambda *a, **k: None), \
         mock.patch.object(remote, "load_open_finds", lambda s: [{"id": "olx_9", "url": "dead"}]), \
         mock.patch.object(remote, "mark_checked", lambda i, a: marked.append((i, a))), \
         mock.patch.object(alive, "is_alive", lambda url: False), \
         mock.patch.object(main.olx, "fetch", lambda pages, seen: ads), \
         mock.patch.object(main.ai, "analyze_batch", fake_batch), \
         mock.patch.object(telegram, "send_ad", lambda ad, v, silent: tg.append((ad.ad_id, silent))), \
         mock.patch.object(digest, "maybe_send", lambda *a: tg.append("digest")), \
         mock.patch.object(state, "load", lambda: {"x": 1}), mock.patch.object(state, "save", lambda s: None), \
         mock.patch.object(main.time, "sleep", lambda s: None), \
         mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "schedule"}):
        main.run(dry_run=False)
    # kitten dropped by the word filter; Sofia ad first (reachable), then freshest of the rest
    assert batches == [["2", "1"], ["4"]], batches
    assert tg[0] == ("2", False) and tg[-1] == "digest", tg
    assert marked == [("olx_9", False)]
    st = saved[-1]
    assert st["status"] == "ok" and st["analyzed"] == 3 and st["junk"] == 1 and st["gone_checked"] == 1, st


def test_digest_due():
    from datetime import datetime, timezone
    from finder import digest
    s = {"digest": {"enabled": True, "hour": 21}}
    assert digest.due(s, datetime(2026, 11, 25, 19, 30, tzinfo=timezone.utc)) == "2026-11-25"  # 21:30 Sofia
    assert digest.due(s, datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)) is None
    assert digest.due({"digest": {"enabled": False}}, datetime(2026, 11, 25, 20, 0, tzinfo=timezone.utc)) is None


def test_price_conf_shown():
    from finder.telegram import format_ad
    data = ai._clean({"score": 7, "price_etsy": "—", "price_ebay": "€5–10", "price_conf": "Низкая (догадка)"})
    assert data["price_conf"] == "низкая"
    assert "уверенность низкая" in format_ad(Ad("olx", "1", "Монети", "u"), data)
    assert ai._clean({"score": 1})["price_conf"] == "средняя"


def test_hunt_words():
    from finder import prefilter
    from finder.telegram import format_ad
    prefilter.configure(hunt_words=["Val Saint Lambert", r"\bleica\b"])
    ad = Ad("x", "1", "Oude vaas Val Saint Lambert", "u")
    assert prefilter.hunt_match(ad) == "Val Saint Lambert"
    assert prefilter.hunt_match(Ad("x", "2", "camera", "u", description="Leica M3 body")) == r"\bleica\b"
    assert not prefilter.hunt_match(Ad("x", "3", "Leicaster", "u"))
    assert "Охота" in format_ad(ad, {"score": 2, "hunt": "Val Saint Lambert"})
    prefilter.configure(hunt_words=[])


def test_lots():
    import main
    ads = [Ad("2dehands", f"m{i}", f"Strip {i}", f"u{i}", price=2, seller="77", photos=[f"p{i}"]) for i in range(5)]
    ads += [Ad("2dehands", "m9", "Vaas", "u9", seller="88"), Ad("2dehands", "m10", "Lamp", "u10")]
    out = main.make_lots(ads, 3)
    assert len(out) == 3 and out[0].members and len(out[0].members) == 5 and out[0].price == 10
    assert out[0].photos == ["p0", "p1", "p2", "p3"] and "Strip 4 — 2 грн" in out[0].description
    assert [a.ad_id for a in out[1:]] == ["m9", "m10"]


def test_gemini_503_not_counted():
    os.environ["GEMINI_API_KEY"] = "x"
    ai._dead_models.clear(); ai.fallback_used.clear()
    answers = [FakeResp(503, text="overloaded"), gemini_answer([{"n": 1, "score": 5}])]
    urls = []

    def post(url, **k):
        urls.append(url)
        return answers.pop(0)
    with mock.patch.object(ai.requests, "post", post), \
         mock.patch.object(ai, "_image_part", lambda u: None), mock.patch.object(ai.time, "sleep", lambda s: None):
        before, failed = ai.calls, ai.failed_calls
        out = ai.analyze_batch([Ad("x", "1", "t", "u")], min_interval=0)
    assert out[0]["score"] == 5 and ai.calls - before == 1 and ai.failed_calls - failed == 1
    # the retry went straight to the fallback model, without waiting
    assert ai.DEFAULT_MODEL in urls[0] and ai.DEFAULT_FALLBACKS[0] in urls[1] and ai.DEFAULT_FALLBACKS[0] in ai.fallback_used


def test_fallback_missing_model_skipped():
    os.environ["GEMINI_API_KEY"] = "x"
    ai._dead_models.clear()
    answers = [FakeResp(503, text="overloaded"), FakeResp(404, text="model not found"), gemini_answer([{"n": 1, "score": 4}])]
    with mock.patch.object(ai.requests, "post", lambda *a, **k: answers.pop(0)), \
         mock.patch.object(ai, "_image_part", lambda u: None), mock.patch.object(ai.time, "sleep", lambda s: None):
        out = ai.analyze_batch([Ad("x", "1", "t", "u")], min_interval=0)
    assert out[0]["score"] == 4 and ai.DEFAULT_FALLBACKS[0] in ai._dead_models
    ai._dead_models.clear()


def test_tg_buttons():
    from finder import tgbuttons
    kb = tgbuttons.keyboard("2dehands_m123", chosen="interesting")
    flat = [b for row in kb["inline_keyboard"] for b in row]
    assert len(flat) == 6 and flat[0]["text"].startswith("☑️") and flat[0]["callback_data"] == tgbuttons.PROJECT + "|i|2dehands_m123"
    long_id = "donnerie_" + "x" * 80
    assert tgbuttons.ref(long_id).startswith("h:") and all(len(b["callback_data"].encode()) <= 64 for row in tgbuttons.keyboard(long_id)["inline_keyboard"] for b in row)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
