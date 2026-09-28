"""Windowed Push digest: fetch -> classify -> prioritize -> persist -> query."""

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone

from app.config import CATEGORY_PRIORITY, USE_LIVE_NSE, load_portfolio
from app.db import get_conn
from app.pipeline.classify import classify_batch
from app.sources.nse_client import fetch_announcements
from app.sources.nse_rss import fetch_recent_announcements
from app.sources.filing_search import canonical_url, fetch_search_announcements, search_status, set_fallback_active
from app.sources.seed_data import load_seed_announcements

logger = logging.getLogger(__name__)

NSE_DT_FORMAT = "%d-%b-%Y %H:%M:%S"


def _parse_nse_dt(raw: str) -> str | None:
    try:
        dt = datetime.strptime(raw, NSE_DT_FORMAT).replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
        return dt.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return None


def _company_lookup() -> dict[str, str]:
    data = load_portfolio()
    return {c["ticker"]: c["name"] for c in data["portfolio"] + data["adhoc"]}


def refresh_all(tickers: list[str] | None = None) -> int:
    """Fetch, classify, and persist announcements for the given tickers
    (defaults to the whole portfolio + ad-hoc list). Returns count persisted."""
    portfolio = load_portfolio()
    tickers = tickers or [c["ticker"] for c in portfolio["portfolio"] + portfolio["adhoc"]]
    names = _company_lookup()

    all_items: list[tuple[str, str, dict]] = []
    completed_backfill = False
    if USE_LIVE_NSE:
        companies = [c for c in portfolio["portfolio"] + portfolio["adhoc"] if c["ticker"] in tickers]
        try:
            recent = fetch_recent_announcements(companies)
        except Exception as exc:
            logger.warning("NSE RSS unavailable: %s", exc)
            recent = None
        with get_conn() as conn:
            backfilled = conn.execute("SELECT 1 FROM migrations WHERE name = 'nse_backfill'").fetchone()
            attempt = conn.execute("SELECT attempted_at FROM feed_state WHERE feed = 'filings_backfill'").fetchone()
            due = not attempt or datetime.now(timezone.utc) - datetime.fromisoformat(attempt[0]) >= timedelta(days=1)
            if not backfilled and due:
                conn.execute("""INSERT INTO feed_state (feed, attempted_at) VALUES ('filings_backfill', ?)
                                ON CONFLICT(feed) DO UPDATE SET attempted_at = excluded.attempted_at""",
                             (datetime.now(timezone.utc).isoformat(),))
        # A shared session backfills just the portfolio. Stop on the first
        # API block rather than retrying blocked sessions for every stock.
        try:
            historical = fetch_announcements(tickers, days=120 if not backfilled and due else 2) if (not backfilled and due) or recent is None else []
        except Exception:
            logger.warning("NSE historical filings unavailable")
            historical = []
        completed_backfill = bool(historical) and not backfilled and due
        if recent is None and not historical:
            for item in fetch_search_announcements(companies):
                all_items.append((item["symbol"], "web_search", item))
        else:
            set_fallback_active(False)
        for source, items in (("nse_live", historical), ("nse_rss", recent or [])):
            for item in items:
                if item.get("symbol") in tickers:
                    all_items.append((item["symbol"], source, item))
        # Retry historical backfill daily if it is blocked, while RSS keeps
        # supplying recent filings. Do not silently load sample announcements.
    else:
        for ticker in tickers:
            all_items.extend((ticker, "seed", item) for item in load_seed_announcements(ticker))

    prepared = {}
    with get_conn() as conn:
        for ticker, source, item in all_items:
            published = _parse_nse_dt(item.get("an_dt", "")) or item.get("sort_date")
            if not published:
                continue
            if item.get("attchmntFile"):
                item = {**item, "attchmntFile": canonical_url(item["attchmntFile"]) or item["attchmntFile"]}
            identity = item.get("attchmntFile") or f"{item.get('desc')}|{item.get('attchmntText')}"
            key = hashlib.sha256(f"{ticker}|{identity}|{published}".encode()).hexdigest()
            existing = conn.execute(
                """SELECT id, source FROM announcements WHERE ticker = ? AND attachment_url = ?
                   AND (published_at = ? OR source = 'web_search' OR ? = 'web_search') LIMIT 1""",
                (ticker, item.get("attchmntFile"), published, source),
            ).fetchone() if item.get("attchmntFile") else None
            # Prefer the exchange's precise timestamp and text when it recovers.
            # A date-only search result must not duplicate or replace that filing.
            if existing and existing["source"] == "web_search" and source != "web_search":
                conn.execute("""UPDATE announcements SET published_at=?, headline=?, source=?,
                                exchange='NSE', raw_json=NULL WHERE id=?""",
                             (published, item.get("attchmntText") or item.get("desc"), source, existing["id"]))
            ann_id = existing["id"] if existing else f"{ticker}:{key}"
            if conn.execute("SELECT 1 FROM announcements WHERE id = ?", (ann_id,)).fetchone():
                continue
            item = {**item, "seq_id": ann_id}
            prepared[ann_id] = (ticker, source, item, published)
    categories = {}
    items = [p[2] for p in prepared.values()]
    for offset in range(0, len(items), 40):
        categories.update(classify_batch(items[offset:offset + 40]))

    count = 0
    with get_conn() as conn:
        for ann_id, (ticker, source, item, published_at) in prepared.items():
            category = categories.get(ann_id, "unclassified")
            priority = CATEGORY_PRIORITY.get(category, "low")
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
                    item.get("exchange", "NSE"),
                    published_at,
                    item.get("attchmntText") or item.get("desc"),
                    category,
                    priority,
                    item.get("attchmntFile"),
                    source,
                    json.dumps(item["search_metadata"]) if item.get("search_metadata") else None,
                ),
            )
            count += 1
        if completed_backfill:
            conn.execute("INSERT OR IGNORE INTO migrations VALUES ('nse_backfill')")
    return count


def get_digest(window_hours: int = 24) -> dict:
    """Announcements within the window, grouped by priority. `routine`
    (low priority + unclassified) is returned separately so the UI can
    collapse it by default."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM announcements
            WHERE published_at >= ? OR
                  (source = 'web_search' AND julianday(published_at, '+1 day') > julianday(?))
            ORDER BY priority = 'high' DESC, priority = 'medium' DESC, published_at DESC
            """,
            (cutoff, cutoff),
        ).fetchall()
        latest = conn.execute("SELECT MAX(published_at) FROM announcements WHERE source != 'seed'").fetchone()[0]
        has_history = conn.execute("SELECT 1 FROM migrations WHERE name = 'nse_backfill'").fetchone() is not None

    high, medium, routine = [], [], []
    for r in rows:
        d = dict(r)
        d["search_metadata"] = json.loads(d["raw_json"]) if d["source"] == "web_search" and d["raw_json"] else {}
        if USE_LIVE_NSE and d["source"] == "seed":
            continue
        if d["priority"] == "high":
            high.append(d)
        elif d["priority"] == "medium":
            medium.append(d)
        else:
            routine.append(d)

    return {"high": high, "medium": medium, "routine": routine, "window_hours": window_hours,
            "latest_filing": latest, "has_history": has_history, "live_mode": USE_LIVE_NSE,
            "filing_search": search_status()}
