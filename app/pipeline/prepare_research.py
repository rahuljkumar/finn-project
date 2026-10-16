"""Prepare Research once per saved evidence revision, away from page requests.

Run ``python -m app.pipeline.prepare_research`` for the portfolio, or append
ticker symbols to resume a subset. Document sync must have saved the sources
first. Each successful section is committed separately and reused on reruns.
"""

import argparse
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from app.config import REASONING_MODEL, load_portfolio
from app.db import init_db
from app.jobs import get_job, start_job
from app.pipeline.pull_pipeline import (
    CATEGORY_PROMPTS,
    corpus_revision,
    document_status,
    generate_research_section,
    get_snapshot,
)
from app.pipeline.research_cache import cached_research, saved_research, saved_research_sections

logger = logging.getLogger(__name__)
RESEARCH_CATEGORIES = (*CATEGORY_PROMPTS, "promised_vs_delivered")
# Preparing the full portfolio cannot fill either the interactive answer pool
# or the automatic Digest queue. One company at a time also bounds RAG memory.
_research_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="finn-research")
RETRY_AFTER = 900


def _select_tickers(tickers: list[str] | None = None) -> list[str]:
    if tickers is None:
        tickers = [company["ticker"] for company in load_portfolio()["portfolio"]]
    normalized = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers))
    if not normalized or any(not re.fullmatch(r"[A-Z0-9&\-]{1,20}", ticker) for ticker in normalized):
        raise ValueError("Provide valid NSE ticker symbols.")
    return normalized


def _has_sources(ticker: str) -> bool:
    if document_status(ticker)["ready"]:
        return True
    snapshot = get_snapshot(ticker)
    return any(snapshot.get(name) for name in ("profit_loss", "balance_sheet", "cash_flow", "ratios", "quarters"))


def _current(ticker: str, category: str, revision: str) -> bool:
    saved = saved_research(ticker, category, revision=revision)
    return bool(saved and saved.get("saved_at") and not saved.get("stale"))


def _current_sections(ticker: str) -> set[str]:
    saved = saved_research_sections(ticker)
    return {category for category in RESEARCH_CATEGORIES
            if saved.get(category) and saved[category].get("saved_at") and not saved[category].get("stale")}


def research_job_key(ticker: str, revision: str | None = None) -> str:
    return f"research:{REASONING_MODEL}:{ticker}:{revision or corpus_revision(ticker)}"


def prepare_company(ticker: str, *, progress: Callable[[dict], None] | None = None) -> dict:
    """Fill only missing/stale sections; failures keep successful work intact."""
    ticker = _select_tickers([ticker])[0]
    revision = corpus_revision(ticker)
    summary = {"ticker": ticker, "revision": revision, "state": "partial",
               "total": len(RESEARCH_CATEGORIES), "ready": 0, "generated": 0,
               "reused": 0, "pending": [], "failed": []}
    if not _has_sources(ticker):
        summary.update(state="waiting_sources", pending=list(RESEARCH_CATEGORIES))
        return summary

    for category in RESEARCH_CATEGORIES:
        # If sync publishes new evidence partway through, the next queued job
        # should prepare its new revision instead of mixing evidence versions.
        if corpus_revision(ticker) != revision:
            break
        if _current(ticker, category, revision):
            summary["reused"] += 1
            continue
        try:
            result = cached_research(ticker, category,
                                     lambda category=category: generate_research_section(ticker, category))
            if result.get("saved_at") and not result.get("stale"):
                summary["generated"] += 1
            else:
                summary["failed"].append(category)
        except Exception:
            logger.exception("Could not prepare Research for %s/%s", ticker, category)
            summary["failed"].append(category)
        if progress:
            progress({"ticker": ticker, "category": category, "generated": summary["generated"],
                      "failed": category in summary["failed"]})

    final_revision = corpus_revision(ticker)
    summary["revision"] = final_revision
    summary["pending"] = [category for category in RESEARCH_CATEGORIES
                          if not _current(ticker, category, final_revision)]
    summary["ready"] = len(RESEARCH_CATEGORIES) - len(summary["pending"])
    summary["state"] = "complete" if not summary["pending"] else "partial"
    return summary


def request_research_preparation(ticker: str) -> dict | None:
    """Coalesce visitors onto one durable job without delaying their page."""
    ticker = _select_tickers([ticker])[0]
    revision = corpus_revision(ticker)
    if len(_current_sections(ticker)) == len(RESEARCH_CATEGORIES):
        return None
    if not _has_sources(ticker):
        return None
    key = research_job_key(ticker, revision)
    # A ready job may contain partial work after a failed section. Back off
    # between retries rather than retrying paid calls on every view/poll.
    saved = get_job(key)
    max_age = RETRY_AFTER if saved and saved["state"] == "ready" else None
    return start_job(key, "research", lambda: prepare_company(ticker),
                     max_age=max_age, executor=_research_pool)


def request_portfolio_preparation() -> list[dict]:
    jobs = []
    for ticker in _select_tickers():
        job = request_research_preparation(ticker)
        if job is not None:
            jobs.append(job)
    return jobs


def portfolio_preparation_status() -> dict:
    """Cheap, read-only progress for the Research tab; never starts AI work."""
    tickers = _select_tickers()
    total = len(tickers) * len(RESEARCH_CATEGORIES)
    ready = active = failed = 0
    for ticker in tickers:
        current = len(_current_sections(ticker))
        ready += current
        if current == len(RESEARCH_CATEGORIES):
            continue
        job = get_job(research_job_key(ticker))
        if not job:
            continue
        if job["state"] == "running":
            active += 1
        elif job["state"] == "error" or (
                job["state"] == "ready" and (job.get("result") or {}).get("state") != "complete"):
            failed += len(RESEARCH_CATEGORIES) - current
    return {"total": total, "ready": ready, "pending": total - ready,
            "companies": len(tickers), "active": active, "failed": failed,
            "state": "complete" if ready == total else "preparing" if active else "partial"}


def prepare_portfolio(tickers: list[str] | None = None, *,
                      progress: Callable[[dict], None] | None = None) -> dict:
    """Resumable preparation of the portfolio; the ad-hoc watchlist is opt-in."""
    selected = _select_tickers(tickers)
    results = []
    for position, ticker in enumerate(selected, start=1):
        try:
            result = prepare_company(ticker, progress=progress)
        except Exception:
            logger.exception("Research preparation failed for %s", ticker)
            result = {"ticker": ticker, "state": "partial", "ready": 0, "generated": 0,
                      "reused": 0, "total": len(RESEARCH_CATEGORIES),
                      "pending": list(RESEARCH_CATEGORIES), "failed": list(RESEARCH_CATEGORIES)}
        results.append(result)
        if progress:
            progress({"ticker": ticker, "position": position, "companies": len(selected),
                      "state": result["state"], "ready": result["ready"], "total": result["total"]})
    incomplete = [result["ticker"] for result in results if result["state"] != "complete"]
    return {"state": "partial" if incomplete else "complete", "companies": len(selected),
            "total": len(selected) * len(RESEARCH_CATEGORIES),
            "ready": sum(result["ready"] for result in results),
            "generated": sum(result["generated"] for result in results),
            "reused": sum(result["reused"] for result in results),
            "incomplete": incomplete, "results": results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare and persist all nine Research sections from saved reports.")
    parser.add_argument("tickers", nargs="*", help="Optional ticker subset; defaults to the 20 portfolio stocks.")
    args = parser.parse_args(argv)
    try:
        tickers = _select_tickers(args.tickers or None)
    except ValueError as exc:
        parser.error(str(exc))
    init_db()
    print(f"Preparing {len(tickers)} companies, {len(RESEARCH_CATEGORIES)} sections each, from saved sources.", flush=True)

    def progress(event):
        if "category" in event:
            state = "retry needed" if event["failed"] else "saved"
            print(f"  {event['ticker']}: {event['category']} — {state}", flush=True)
        else:
            print(f"[{event['position']}/{event['companies']}] {event['ticker']}: "
                  f"{event['ready']}/{event['total']} ready ({event['state']})", flush=True)

    result = prepare_portfolio(tickers, progress=progress)
    print(f"Research {result['state']}: {result['ready']}/{result['total']} sections ready; "
          f"{result['generated']} generated, {result['reused']} reused.", flush=True)
    if result["incomplete"]:
        print(f"Retry needed: {', '.join(result['incomplete'])}. Sync missing reports first; "
              "rerunning reuses completed sections.", flush=True)
    return 1 if result["incomplete"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
