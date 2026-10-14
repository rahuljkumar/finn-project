"""Threshold-gated explanations of the completed session on each alert."""

import hashlib
import json

from app.config import WEB_SEARCH_MODEL
from app.jobs import get_job, start_job
from app.llm.client import search_with_sources


def search_enabled(volume_filter: float, price_filter: float) -> bool:
    return volume_filter >= 2 and price_filter >= 5


def qualifies(alert: dict, volume_filter: float, price_filter: float) -> bool:
    if not search_enabled(volume_filter, price_filter):
        return False
    volume = alert.get("volume_ratio")
    price = alert.get("pct_change")
    return ((volume is not None and volume > volume_filter)
            or (price is not None and abs(price) > price_filter))


def generate_explanation(alert: dict) -> dict:
    result = search_with_sources(
        f"Explain the EOD move of {alert.get('company') or alert['ticker']} (NSE ticker {alert['ticker']}) "
        f"on trading date {alert['date']}. Actual price change: {alert.get('pct_change')}%; "
        f"volume multiple: {alert.get('volume_ratio')}. Find relevant reporting around that session. "
        "Check both article publication time and underlying event date. Later reporting may describe an earlier "
        "event, but an event after the session cannot have caused its move. Exclude other companies and stale news. "
        "Use reported only when reporting explicitly attributes this move to an event; related for plausible "
        "but unproven connections. Do not infer causation solely from coincident dates. Use no_news otherwise."
    )
    labels = {"reported": "Reported catalyst", "related": "Possibly related news", "no_news": "No reliable explanation found"}
    if result["status"] not in labels:
        raise ValueError("Invalid alert evidence status")
    result.update(status_label=labels[result["status"]], trading_date=alert["date"])
    if result["status"] == "no_news":
        result["content"] = "No reliable reporting was found that explains this session's move."
    return result


def request_explanation(alert: dict, volume_filter: float, price_filter: float, retry=False) -> dict | None:
    # Both entry and worker enforce the gate. Low filters never create paid searches.
    if not qualifies(alert, volume_filter, price_filter):
        return None
    fingerprint = hashlib.sha256(json.dumps(
        [alert["date"], alert.get("close"), alert.get("volume_ratio"), alert.get("pct_change"), WEB_SEARCH_MODEL]
    ).encode()).hexdigest()[:24]
    key = f"news:{alert['ticker']}:{fingerprint}"
    saved = get_job(key)
    expiry = 3600 if saved and (saved.get("result") or {}).get("status") == "no_news" else 21600
    def work():
        if not qualifies(alert, volume_filter, price_filter):
            raise ValueError("Search disabled for these filters")
        return generate_explanation(alert)
    return start_job(key, "news", work, max_age=0 if retry else expiry, retry=retry)
