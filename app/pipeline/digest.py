"""Windowed Push digest: fetch -> classify -> prioritize -> persist -> query."""

import hashlib
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from time import perf_counter

from app.config import CATEGORY_PRIORITY, USE_LIVE_NSE, load_portfolio
from app.db import get_conn
from app.pipeline.classify import classify_batch, rule_classify
from app.sources.nse_client import fetch_announcements
from app.sources.nse_rss import fetch_recent_announcements
from app.sources.filing_search import canonical_url, fetch_search_announcements, search_status, set_fallback_active
from app.sources.seed_data import load_seed_announcements

logger = logging.getLogger(__name__)
_ingest_guard = threading.Lock()

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
    """Save recent filings immediately; maintenance runs in separate jobs."""
    portfolio = load_portfolio()
    tickers = tickers or [c["ticker"] for c in portfolio["portfolio"] + portfolio["adhoc"]]
    all_items: list[tuple[str, str, dict]] = []
    if USE_LIVE_NSE:
        companies = [c for c in portfolio["portfolio"] + portfolio["adhoc"] if c["ticker"] in tickers]
        started = perf_counter()
        try:
            recent = fetch_recent_announcements(companies)
        except Exception as exc:
            logger.warning("NSE RSS unavailable: %s", exc)
            recent = None
        logger.info("Filings RSS finished in %.2fs (%s items)", perf_counter() - started,
                    len(recent) if recent is not None else "unavailable")
        # Only use the short API fallback when RSS is unavailable. The full
        # 120-day backfill must never hold up a normal recent-feed refresh.
        try:
            historical = fetch_announcements(tickers, days=2) if recent is None else []
        except Exception:
            logger.warning("NSE historical filings unavailable")
            historical = []
        if recent is None and not historical:
            for item in fetch_search_announcements(companies):
                all_items.append((item["symbol"], "web_search", item))
        else:
            set_fallback_active(False)
        for source, items in (("nse_live", historical), ("nse_rss", recent or [])):
            for item in items:
                if item.get("symbol") in tickers:
                    all_items.append((item["symbol"], source, item))
    else:
        for ticker in tickers:
            all_items.extend((ticker, "seed", item) for item in load_seed_announcements(ticker))

    return _persist_announcements(all_items)


def backfill_filings() -> int:
    """Load historical coverage independently of recent-feed refreshes."""
    if not USE_LIVE_NSE:
        return 0
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM migrations WHERE name='nse_backfill'").fetchone():
            return 0
    tickers = list(_company_lookup())
    historical = fetch_announcements(tickers, days=120)
    if not historical:
        raise RuntimeError("Historical filings are unavailable; recent filings are retained")
    count = _persist_announcements([(item["symbol"], "nse_live", item)
                                    for item in historical if item.get("symbol") in tickers])
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO migrations VALUES ('nse_backfill')")
    return count


def _persist_announcements(all_items: list[tuple[str, str, dict]]) -> int:
    """Commit rule categories and explicit pending inputs without model calls.

    Recent ingestion and historical backfill can overlap. Keep deduplication
    and insertion together, with no network work inside this short lock.
    """
    names = _company_lookup()
    count = 0
    with _ingest_guard, get_conn() as conn:
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
            ann_id = existing["id"] if existing else f"{ticker}:{key}"
            category = rule_classify(item)
            pending = json.dumps({"seq_id": ann_id, "desc": item.get("desc", ""),
                                  "attchmntText": item.get("attchmntText", "")}) if category is None else None
            category = category or "unclassified"
            priority = CATEGORY_PRIORITY.get(category, "low")
            # Exchange recovery replaces approximate search metadata and
            # categorizes the authoritative description without duplicating it.
            if existing and existing["source"] == "web_search" and source != "web_search":
                conn.execute("""UPDATE announcements SET published_at=?, headline=?, source=?,
                                exchange='NSE', raw_json=NULL, category=?, priority=?, classification_input=? WHERE id=?""",
                             (published, item.get("attchmntText") or item.get("desc"), source,
                              category, priority, pending, ann_id))
            if conn.execute("SELECT 1 FROM announcements WHERE id = ?", (ann_id,)).fetchone():
                continue
            conn.execute(
                """INSERT INTO announcements
                    (id, ticker, company, exchange, published_at, headline, category,
                     priority, attachment_url, source, raw_json, classification_input)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ann_id, ticker, names.get(ticker, ticker), item.get("exchange", "NSE"), published,
                 item.get("attchmntText") or item.get("desc"), category, priority, item.get("attchmntFile"), source,
                 json.dumps(item["search_metadata"]) if item.get("search_metadata") else None, pending),
            )
            count += 1
    return count


def classify_pending_announcements() -> int:
    """Enrich already-visible filings in bounded batches, newest first.

    Inputs remain in SQLite until their result is committed, so a restart
    resumes unfinished work. A legitimate unclassified result is terminal.
    """
    count = 0
    while True:
        with get_conn() as conn:
            rows = conn.execute("""SELECT id, classification_input FROM announcements
                WHERE classification_input IS NOT NULL ORDER BY published_at DESC LIMIT 40""").fetchall()
        if not rows:
            return count
        started = perf_counter()
        categories = classify_batch([json.loads(row["classification_input"]) for row in rows])
        with get_conn() as conn:
            for row in rows:
                category = categories.get(row["id"], "unclassified")
                updated = conn.execute("""UPDATE announcements SET category=?, priority=?, classification_input=NULL
                    WHERE id=? AND classification_input=?""",
                    (category, CATEGORY_PRIORITY.get(category, "low"), row["id"], row["classification_input"]))
                count += updated.rowcount
        logger.info("Classified %d saved filings in %.2fs", len(rows), perf_counter() - started)


def get_digest(window_hours: int = 24, ticker: str = "", query: str = "") -> dict:
    """Announcements within the window, grouped by priority. `routine`
    (low priority + unclassified) is returned separately so the UI can
    collapse it by default."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=window_hours)).isoformat()
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM announcements
            WHERE julianday(published_at) <= julianday(?) AND (published_at >= ? OR
                  (source = 'web_search' AND julianday(published_at, '+1 day') > julianday(?)))
            ORDER BY priority = 'high' DESC, priority = 'medium' DESC, published_at DESC
            """,
            (now.isoformat(), cutoff, cutoff),
        ).fetchall()
        latest = conn.execute("SELECT MAX(published_at) FROM announcements WHERE source != 'seed'").fetchone()[0]
        has_history = conn.execute("SELECT 1 FROM migrations WHERE name = 'nse_backfill'").fetchone() is not None

    high, medium, routine, pending = [], [], [], []
    for r in rows:
        d = dict(r)
        d["search_metadata"] = json.loads(d["raw_json"]) if d["source"] == "web_search" and d["raw_json"] else {}
        if USE_LIVE_NSE and d["source"] == "seed":
            continue
        if ticker and d["ticker"] != ticker:
            continue
        if query and query.casefold() not in " ".join(str(d.get(k) or "") for k in ("headline", "company", "ticker", "category")).casefold():
            continue
        if d.pop("classification_input", None) is not None:
            pending.append(d)
        elif d["priority"] == "high":
            high.append(d)
        elif d["priority"] == "medium":
            medium.append(d)
        else:
            routine.append(d)

    return {"high": high, "medium": medium, "routine": routine, "pending": pending, "window_hours": window_hours, "as_of": now.isoformat(),
            "latest_filing": latest, "has_history": has_history, "live_mode": USE_LIVE_NSE,
            "filing_search": search_status()}
