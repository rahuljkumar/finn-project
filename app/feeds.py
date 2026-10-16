"""Background refresh with one in-flight job per feed and durable health.

The service uses a single Uvicorn process. A paid always-on instance polls
without user traffic; free instances can only poll while Render keeps them up.
"""

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from time import perf_counter

from app.db import get_conn
from app.config import DOCUMENT_SYNC_HOUR, DOCUMENT_SYNC_MINUTE

logger = logging.getLogger(__name__)
_guard = threading.Lock()
_running = set()
INTERVALS = {"filings": 300, "prices": 1800, "documents": 86400, "research": 900,
             "filings_backfill": 86400, "filings_classify": 300}


def feed_status(feed: str) -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM feed_state WHERE feed = ?", (feed,)).fetchone()
        pending = feed == "filings" and conn.execute(
            "SELECT 1 FROM announcements WHERE classification_input IS NOT NULL LIMIT 1").fetchone() is not None
        maintenance = {r["feed"]: r["error"] for r in conn.execute(
            "SELECT feed,error FROM feed_state WHERE feed IN ('filings_classify','filings_backfill')")} if feed == "filings" else {}
    result = dict(row) if row else {"attempted_at": None, "succeeded_at": None, "error": None, "summary": None}
    result["summary"] = json.loads(result["summary"]) if result["summary"] else None
    with _guard:
        result["running"] = feed in _running
        result["backfill_running"] = feed == "filings" and "filings_backfill" in _running
        result["classification_running"] = feed == "filings" and "filings_classify" in _running
    result["classification_error"] = maintenance.get("filings_classify")
    result["backfill_error"] = maintenance.get("filings_backfill")
    result["polling"] = (result["running"] or result["backfill_running"] or result["classification_running"]
                         or (pending and not result["classification_error"]))
    return result


def _job(feed: str):
    from app.pipeline.alerts import refresh_price_history
    from app.pipeline.digest import backfill_filings, classify_pending_announcements, refresh_all

    started = perf_counter()
    succeeded = False
    try:
        if feed == "filings":
            summary = {"added": refresh_all()}
        elif feed == "filings_backfill":
            summary = {"added": backfill_filings()}
        elif feed == "filings_classify":
            summary = {"classified": classify_pending_announcements()}
        elif feed == "documents":
            from app.pipeline.pull_pipeline import sync_portfolio
            summary = sync_portfolio(prepare_sections=True)
        elif feed == "research":
            from app.pipeline.prepare_research import request_portfolio_preparation
            jobs = request_portfolio_preparation()
            summary = {"queued": sum(bool(job and job.get("state") == "running") for job in jobs)}
        else:
            summary = refresh_price_history()
        error = (("Some reports could not be indexed; saved reports are retained." if feed == "documents"
                  else "Some stock prices could not be refreshed; saved data is retained.") if summary.get("failed") else None)
        with get_conn() as conn:
            conn.execute("UPDATE feed_state SET succeeded_at = ?, error = ?, summary = ? WHERE feed = ?",
                         (datetime.now(timezone.utc).isoformat(), error, json.dumps(summary), feed))
        succeeded = True
        if feed in {"filings", "filings_backfill", "filings_classify"}:
            from app.pipeline.summaries import prepare_presets
            try:
                prepare_presets()
            except Exception:
                logger.exception("Could not queue Digest summaries; saved filings remain available")
    except Exception:
        logger.exception("%s refresh failed", feed)
        # UI gets a useful fixed message; don't expose upstream response bodies.
        with get_conn() as conn:
            conn.execute("UPDATE feed_state SET error = ? WHERE feed = ?",
                         ("Historical filings are unavailable. Recent results remain available." if feed == "filings_backfill" else
                          "Some new filings could not be categorized yet. Saved disclosures remain available; refresh will retry."
                          if feed == "filings_classify" else
                          "Live source unavailable. Saved data is retained; refresh will retry automatically.", feed))
    finally:
        # Source refreshes finish independently of slower maintenance. Keep
        # the source marked running until maintenance is scheduled so a page
        # cannot stop polling in the gap between the two jobs.
        if feed in {"filings", "filings_backfill"}:
            try:
                request_refresh("filings_classify")
                if feed == "filings":
                    request_refresh("filings_backfill")
            except Exception:
                logger.exception("Could not schedule filings maintenance")
        with _guard:
            _running.discard(feed)
        # An ingestion job can add pending inputs after the classifier's last
        # empty read but before it unregisters. Recheck after releasing its
        # in-flight slot; provider failures retain their retry cooldown.
        if feed == "filings_classify" and succeeded:
            try:
                request_refresh("filings_classify")
            except Exception:
                logger.exception("Could not resume filings classification")
        logger.info("%s job finished in %.2fs", feed, perf_counter() - started)


def request_refresh(feed: str, force: bool = False) -> bool:
    if feed not in INTERVALS:
        raise ValueError("Unknown feed")
    now = datetime.now(timezone.utc)
    with _guard:
        if feed in _running:
            return False
        with get_conn() as conn:
            if feed == "filings_backfill":
                from app.config import USE_LIVE_NSE
                if not USE_LIVE_NSE or conn.execute("SELECT 1 FROM migrations WHERE name='nse_backfill'").fetchone():
                    return False
            if feed == "filings_classify" and not conn.execute(
                    "SELECT 1 FROM announcements WHERE classification_input IS NOT NULL LIMIT 1").fetchone():
                return False
            row = conn.execute("SELECT attempted_at,error FROM feed_state WHERE feed = ?", (feed,)).fetchone()
            # Even manual clicks get a short cooldown to avoid provider bans.
            wait = 60 if force else INTERVALS[feed]
            # New filings must not wait five minutes behind the previous
            # successful batch. The in-flight guard still coalesces callers.
            if feed == "filings_classify":
                wait = 60 if row and row[1] else 0
            if row and row[0]:
                attempted = datetime.fromisoformat(row[0])
                if feed == "documents" and not force:
                    if attempted >= document_schedule_slot(now) and (not row[1] or now - attempted < timedelta(hours=1)):
                        return False
                elif now - attempted < timedelta(seconds=wait):
                    return False
            conn.execute("""INSERT INTO feed_state (feed, attempted_at) VALUES (?, ?)
                            ON CONFLICT(feed) DO UPDATE SET attempted_at = excluded.attempted_at""",
                         (feed, now.isoformat()))
        _running.add(feed)
    threading.Thread(target=_job, args=(feed,), daemon=True, name=f"finn-{feed}").start()
    return True


def document_schedule_slot(now: datetime) -> datetime:
    ist = timezone(timedelta(hours=5, minutes=30))
    local = now.astimezone(ist)
    slot = local.replace(hour=DOCUMENT_SYNC_HOUR, minute=DOCUMENT_SYNC_MINUTE, second=0, microsecond=0)
    return (slot if local >= slot else slot - timedelta(days=1)).astimezone(timezone.utc)


def poll_feeds(stop: threading.Event):
    # A recent scheduling timestamp must not delay unfinished work after a
    # restart. Per-company job keys coalesce this with views and report sync.
    if not stop.is_set():
        try:
            from app.pipeline.prepare_research import request_portfolio_preparation
            request_portfolio_preparation()
        except Exception:
            logger.exception("Could not resume saved Research preparation")
    while not stop.is_set():
        for feed in INTERVALS:
            # A full historical pass is requested after recent ingestion,
            # and has its own durable once-per-day cooldown.
            if feed == "filings_backfill":
                continue
            try:
                request_refresh(feed)
            except Exception:
                logger.exception("Could not schedule %s refresh", feed)
        stop.wait(30)
