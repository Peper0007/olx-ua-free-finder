"""Free-stuff finder UA: OLX.ua («Віддам безкоштовно») → Gemini → Telegram (+ Firestore dashboard).

Usage:
  python main.py                 normal run (used by GitHub Actions)
  python main.py --dry-run       print results instead of sending to Telegram, don't save state
  python main.py --chat-id       print your TELEGRAM_CHAT_ID (write to the bot first)
  python main.py --test          send a test message to Telegram

Settings come from the dashboard (Firestore config/settings) when FIREBASE_SERVICE_ACCOUNT
is set; otherwise from env vars / built-in defaults (see finder/remote.py).
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from finder import ai, common, prefilter, remote, state, telegram
from finder.dedup import fingerprint, title_fingerprint
from finder.common import Ad, log
from finder.sources import alo, bazar, olx


def collect(settings: dict, report=lambda **kw: None) -> tuple[list[Ad], dict, list[str]]:
    src = settings["sources"]
    plan = []
    if src["olx"].get("enabled", True):
        plan.append(("olx", lambda: olx.fetch(int(src["olx"].get("pages", 25)))))
    if src.get("bazar", {}).get("enabled", False):
        plan.append(("bazar", lambda: bazar.fetch(int(src["bazar"].get("pages", 10)))))
    if src.get("alo", {}).get("enabled", False):
        plan.append(("alo", lambda: alo.fetch(src["alo"].get("queries") or None)))

    ads: list[Ad] = []
    counts: dict[str, int] = {}
    failed: list[str] = []
    for i, (name, fn) in enumerate(plan, 1):
        report(phase="collect", source=name, step=f"{i}/{len(plan)}", collected=len(ads))
        try:
            got = fn()
        except Exception as exc:
            log.exception("%s crashed: %s", name, exc)
            got = []
        counts[name] = len(got)
        if not got:
            failed.append(name)
        ads.extend(got)
    unique: dict[str, Ad] = {}
    for ad in ads:
        unique.setdefault(ad.key, ad)
    return list(unique.values()), counts, failed


def run(dry_run: bool) -> int:
    settings = remote.load_settings()
    trigger = "manual" if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" else "schedule"
    if os.environ.get("GITHUB_EVENT_NAME") is None:
        trigger = "local"

    if settings.get("paused") and trigger == "schedule":
        log.info("Bot is paused in the dashboard — skipping scheduled run")
        return 0

    prefilter.configure(settings.get("skip_words"), settings.get("keep_words"), settings.get("skip_url_parts"))
    min_score = int(settings.get("min_score", 6))
    max_ai = int(settings.get("max_ai_per_run", 100))
    telegram_on = bool(settings.get("telegram_enabled", True)) and not dry_run

    run_id = None if dry_run else remote.start_run(trigger)
    stats: dict = {"status": "running", "errors": []}

    seen = state.load()
    first_run = not seen
    now = time.time()
    sent = analyzed = 0
    cur = {"source": ""}

    def report(force: bool = False, **fields):
        if "source" in fields:
            cur["source"] = fields["source"]
        remote.progress(run_id, force=force, **fields)

    def on_fetch(url: str):
        page = ""
        m = __import__("re").search(r"[?&]page=(\d+)", url)
        if m:
            page = m.group(1)
        report(phase="collect", source=cur["source"], url=url, page=page)

    common.on_fetch = on_fetch
    try:
        report(force=True, phase="start")
        ads, counts, failed = collect(settings, report)
        common.on_fetch = None
        new = [a for a in ads if a.key not in seen]
        log.info("Collected %s ads, %s new%s", len(ads), len(new), " (first run)" if first_run else "")

        candidates: list[Ad] = []
        junk = dups = blocked = 0
        # items the owner marked spam / «уже забрали» / «забрал» are never sent again, even if reposted
        blocked_keys, blocked_fps, spam_titles = remote.load_blocked()
        batch_fps: set[str] = set()
        for ad in new:
            if prefilter.is_junk(ad):
                seen[ad.key] = now
                junk += 1
                continue
            fp = fingerprint(ad)
            if ad.key in blocked_keys or fp in blocked_fps or title_fingerprint(ad.title) in spam_titles:
                seen[ad.key] = now
                blocked += 1
                continue
            if fp in seen or fp in batch_fps:
                # same item already analyzed (reposted with a new id or listed on another site)
                seen[ad.key] = now
                dups += 1
                continue
            batch_fps.add(fp)
            candidates.append(ad)
        log.info("%s candidates after keyword pre-filter (%s junk, %s duplicates, %s blocked by owner)",
                 len(candidates), junk, dups, blocked)
        stats.update(counts=counts, collected=len(ads), new=len(new), junk=junk, duplicates=dups + blocked, blocked=blocked,
                     candidates=len(candidates), failed_sources=failed)

        # Free stuff goes fast: analyze the freshest ads first (OLX has exact time, Bazar says "днес").
        def freshness(ad: Ad) -> float:
            if ad.posted_at:
                return ad.posted_at
            return now - 3600 if "днес" in (ad.posted or "").lower() else 0.0
        candidates.sort(key=freshness, reverse=True)
        examples = remote.load_feedback_examples() if settings.get("learn_from_marks", True) else ""
        if examples:
            log.info("Using owner feedback examples (%s lines)", examples.count("\n- "))
        todo = candidates[:max_ai]
        report(force=True, phase="filter", collected=len(ads), new=len(new), junk=junk, duplicates=dups,
               candidates=len(candidates), todo=len(todo))
        # GitHub kills the job at 30 min; stop analyzing in time to save results and seen.json cleanly
        budget = float(os.environ.get("RUN_BUDGET_MIN", "24")) * 60
        for i, ad in enumerate(todo, 1):
            if time.time() - now > budget:
                log.warning("Time budget reached after %s ads; the rest waits for the next run", i - 1)
                stats["errors"].append(f"Время запуска вышло — оценено {i - 1} из {len(todo)}, остальное в следующий раз")
                break
            report(phase="analyze", done=i - 1, todo=len(todo), source=ad.source, title=ad.title[:120],
                   url=ad.url, hits=sent)
            if ad.source == "bazar" and ad.needs_details:
                bazar.enrich(ad)
            try:
                verdict = ai.analyze(ad, model=settings.get("model"), criteria=settings.get("criteria"),
                                     examples=examples)
            except ai.QuotaExhausted:
                log.warning("Gemini daily quota exhausted; the rest waits for the next run")
                stats["errors"].append("Дневной лимит Gemini исчерпан")
                break
            analyzed += 1
            if verdict is None:
                continue  # transient failure: retry next run
            seen[ad.key] = now
            seen[fingerprint(ad)] = now
            score = verdict.get("score", 0)
            log.info("[%s/10] %s — %s", score, ad.title, ad.url)
            is_hit = score >= min_score
            if is_hit:
                if dry_run:
                    print("\n" + telegram.format_ad(ad, verdict) + "\n")
                elif telegram_on:
                    try:
                        telegram.send_ad(ad, verdict, silent=score < int(settings.get("loud_score", 8)))
                        time.sleep(1)
                    except Exception as exc:
                        log.error("Telegram send failed: %s", exc)
                        stats["errors"].append(f"Telegram: {exc}")
                sent += 1
            if not dry_run:
                remote.save_find(ad, verdict, sent=is_hit and telegram_on, run_id=run_id)

        if first_run:
            for ad in candidates:
                seen.setdefault(ad.key, now)

        if len(failed) == len(counts) and counts and telegram_on:
            telegram.send_text("⚠️ Поиск бесплатных объявлений: ни один сайт не ответил. "
                               "Возможно, блокировка или сайты изменили вёрстку — проверь логи в GitHub Actions.")
        elif failed:
            log.warning("No ads from: %s", ", ".join(failed))
        stats["status"] = "ok"
    except Exception as exc:
        stats["status"] = "failed"
        stats["errors"].append(f"{type(exc).__name__}: {exc}"[:300])
        raise
    finally:
        if not dry_run:
            state.save(seen)
        stats.update(analyzed=analyzed, sent=sent, ai_calls=ai.calls, model=settings.get("model"), backlog=max(0, stats.get("candidates", 0) - analyzed))
        remote.finish_run(run_id, stats)
        log.info("Done: analyzed %s, sent %s", analyzed, sent)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--chat-id", action="store_true")
    p.add_argument("--test", action="store_true")
    args = p.parse_args()

    if args.chat_id:
        telegram.print_chat_ids()
        return 0
    if args.test:
        telegram.send_text("✅ Бот работает. Находки будут приходить сюда 4 раза в день.")
        return 0
    return run(args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
