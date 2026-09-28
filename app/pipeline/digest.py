"""Windowed Push digest: fetch -> classify -> prioritize -> persist -> query."""

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from app.config import CATEGORY_PRIORITY, USE_LIVE_NSE, load_portfolio
from app.db import get_conn
from app.pipeline.classify import classify_batch
from app.sources.nse_client import fetch_announcements
from app.sources.seed_data import load_seed_announcements

logger = logging.getLogger(__name__)

NSE_DT_FORMAT = "%d-%b-%Y %H:%M:%S"


def _parse_nse_dt(raw: str) -> str | None:
    try:
        dt = datetime.strptime(raw, NSE_DT_FORMAT).replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except (ValueError, TypeError):
        return None


def _company_lookup() -> dict[str, str]:
    data = load_portfolio()
    return {c["ticker"]: c["name"] for c in data["portfolio"] + data["adhoc"]}


def fetch_with_fallback(ticker: str) -> tuple[list[dict], str]:
    """Try live NSE first, fall back to curated seed data. Returns (items, source)."""
    if USE_LIVE_NSE:
        live = fetch_announcements(ticker)
        if live:
            return live, "nse_live"
    return load_seed_announcements(ticker), "seed"


def refresh_all(tickers: list[str] | None = None) -> int:
    """Fetch, classify, and persist announcements for the given tickers
    (defaults to the whole portfolio + ad-hoc list). Returns count persisted."""
    portfolio = load_portfolio()
    tickers = tickers or [c["ticker"] for c in portfolio["portfolio"] + portfolio["adhoc"]]
    names = _company_lookup()

    all_items: list[tuple[str, str, dict]] = []  # (ticker, source, raw item)
    # Modest concurrency (not all 25 at once) -- NSE self-throttles per
    # session to ~3 req/s and each ticker fetch does its own session
    # warm-up, so this trades some of that budget for wall-clock time
    # without blasting the endpoint with 25 simultaneous sessions.
    with ThreadPoolExecutor(max_workers=4) as pool:
        fetched = pool.map(fetch_with_fallback, tickers)
    for ticker, (items, source) in zip(tickers, fetched):
        for item in items:
            all_items.append((ticker, source, item))

    categories = classify_batch([item for _, _, item in all_items])

    count = 0
    with get_conn() as conn:
        for ticker, source, item in all_items:
            seq_id = str(item.get("seq_id") or item.get("id") or hash(json_key(item)))
            ann_id = f"{ticker}:{seq_id}"
            category = categories.get(seq_id, "unclassified")
            priority = CATEGORY_PRIORITY.get(category, "low")
            published_at = _parse_nse_dt(item.get("an_dt", "")) or item.get("sort_date")

            conn.execute(
                """
                INSERT INTO announcements
                    (id, ticker, company, exchange, published_at, headline, category,
                     priority, attachment_url, source, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    category=excluded.category,
                    priority=excluded.priority,
                    published_at=excluded.published_at
                """,
                (
                    ann_id,
                    ticker,
                    names.get(ticker, ticker),
                    "NSE",
                    published_at,
                    item.get("attchmntText") or item.get("desc"),
                    category,
                    priority,
                    item.get("attchmntFile"),
                    source,
                    None,
                ),
            )
            count += 1
    return count


def json_key(item: dict) -> str:
    return f"{item.get('desc')}|{item.get('an_dt')}|{item.get('symbol')}"


def get_digest(window_hours: int = 24) -> dict:
    """Announcements within the window, grouped by priority. `routine`
    (low priority + unclassified) is returned separately so the UI can
    collapse it by default."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM announcements
            WHERE published_at >= ?
            ORDER BY priority = 'high' DESC, priority = 'medium' DESC, published_at DESC
            """,
            (cutoff,),
        ).fetchall()

    high, medium, routine = [], [], []
    for r in rows:
        d = dict(r)
        if d["priority"] == "high":
            high.append(d)
        elif d["priority"] == "medium":
            medium.append(d)
        else:
            routine.append(d)

    return {"high": high, "medium": medium, "routine": routine, "window_hours": window_hours}
