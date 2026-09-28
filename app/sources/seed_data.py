"""Curated fallback announcements, keyed by ticker. Used whenever a live
source (NSE/BSE) returns nothing -- keeps the digest/alerts demo working
regardless of live-scraping flakiness at record time."""

import json
from pathlib import Path

from app.config import SEED_DATA_DIR

ANNOUNCEMENTS_DIR = SEED_DATA_DIR / "announcements"


def load_seed_announcements(ticker: str) -> list[dict]:
    path = ANNOUNCEMENTS_DIR / f"{ticker}.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_seed_announcements(ticker: str, items: list[dict]) -> None:
    ANNOUNCEMENTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ANNOUNCEMENTS_DIR / f"{ticker}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
