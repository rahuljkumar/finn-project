"""Triggered alerts: unusual EOD price/volume moves, plus material
announcement categories, surfaced separately from the routine digest."""

import logging
from datetime import datetime, timedelta, timezone

from app.config import USE_LIVE_NSE, load_portfolio
from app.db import get_conn
from app.sources.yfinance_client import detect_anomaly, refresh_ticker
from app.sources.yfinance_client import persist_history
from app.sources.nse_reports import fetch_portfolio_history

logger = logging.getLogger(__name__)

TRIGGER_CATEGORIES = {"results", "M&A", "management_change", "fund_raise", "litigation"}


def _tickers() -> list[str]:
    portfolio = load_portfolio()
    return [c["ticker"] for c in portfolio["portfolio"] + portfolio["adhoc"]]


def refresh_price_history() -> dict:
    tickers = _tickers()
    official = fetch_portfolio_history(tickers)
    refreshed, failed = [], []
    for ticker in tickers:
        try:
            if ticker in official:
                history = official[ticker]
                persist_history(ticker, history)
            else:
                history = refresh_ticker(ticker)
            if history.empty:
                failed.append(ticker)
            else:
                refreshed.append(ticker)
        except Exception as exc:
            logger.warning("Price refresh failed for %s: %s", ticker, exc)
            failed.append(ticker)
    if tickers and not refreshed:
        raise RuntimeError("Both price sources are unavailable. Previously saved prices are retained.")
    return {"total": len(tickers), "refreshed": len(refreshed), "failed": failed,
            "official": len(official)}


def get_price_data_status() -> dict:
    tickers = _tickers()
    if not tickers:
        return {"total": 0, "loaded": 0, "available": 0, "latest_date": None}
    placeholders = ",".join("?" for _ in tickers)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM prices
            WHERE ticker IN ({placeholders}) AND (ticker, date) IN (
                SELECT ticker, MAX(date) FROM prices GROUP BY ticker
            )
            """,
            tickers,
        ).fetchall()
    available = sum(
        r["pct_change"] is not None
        or (r["volume"] is not None and r["avg_volume_20d"] is not None and r["avg_volume_20d"] > 0)
        for r in rows
    )
    return {
        "total": len(tickers),
        "loaded": len(rows),
        "available": available,
        "latest_date": max((r["date"] for r in rows), default=None),
        "oldest_date": min((r["date"] for r in rows), default=None),
    }


def get_price_alerts(volume_multiple: float | None = None, price_move_pct: float | None = None) -> list[dict]:
    alerts = []
    for ticker in _tickers():
        anomaly = detect_anomaly(ticker, volume_multiple, price_move_pct)
        if anomaly:
            alerts.append({"kind": "price_volume", **anomaly})
    return alerts


def get_category_alerts(window_hours: int = 72) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    placeholders = ",".join("?" for _ in TRIGGER_CATEGORIES)
    with get_conn() as conn:
        live_only = " AND source != 'seed'" if USE_LIVE_NSE else ""
        rows = conn.execute(
            f"""
            SELECT * FROM announcements
            WHERE published_at >= ? AND category IN ({placeholders}){live_only}
            ORDER BY published_at DESC
            """,
            (cutoff, *TRIGGER_CATEGORIES),
        ).fetchall()
    return [{"kind": "announcement", **dict(r)} for r in rows]


def get_all_alerts(
    window_hours: int = 72,
    volume_multiple: float | None = None,
    price_move_pct: float | None = None,
) -> dict:
    return {
        "price_volume": get_price_alerts(volume_multiple, price_move_pct),
        "announcements": get_category_alerts(window_hours),
        "price_data": get_price_data_status(),
    }
