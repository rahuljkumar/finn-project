"""Triggered alerts: unusual EOD price/volume moves, plus material
announcement categories, surfaced separately from the routine digest."""

from datetime import datetime, timedelta, timezone

from app.config import load_portfolio
from app.db import get_conn
from app.sources.yfinance_client import detect_anomaly

TRIGGER_CATEGORIES = {"results", "M&A", "management_change", "fund_raise", "litigation"}


def get_price_alerts(volume_multiple: float | None = None, price_move_pct: float | None = None) -> list[dict]:
    portfolio = load_portfolio()
    tickers = [c["ticker"] for c in portfolio["portfolio"] + portfolio["adhoc"]]
    alerts = []
    for ticker in tickers:
        anomaly = detect_anomaly(ticker, volume_multiple, price_move_pct)
        if anomaly:
            alerts.append({"kind": "price_volume", **anomaly})
    return alerts


def get_category_alerts(window_hours: int = 72) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=window_hours)).isoformat()
    placeholders = ",".join("?" for _ in TRIGGER_CATEGORIES)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT * FROM announcements
            WHERE published_at >= ? AND category IN ({placeholders})
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
    }
