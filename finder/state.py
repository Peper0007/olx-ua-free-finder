"""Remembers which ads were already processed (state/seen.json, committed back by the workflow)."""
from __future__ import annotations

import json
import time
from pathlib import Path

PATH = Path(__file__).resolve().parent.parent / "state" / "seen.json"
KEEP_DAYS = 90


def load() -> dict[str, float]:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save(seen: dict[str, float]) -> None:
    cutoff = time.time() - KEEP_DAYS * 86400
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=0, sort_keys=True), encoding="utf-8")
