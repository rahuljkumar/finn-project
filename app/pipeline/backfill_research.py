"""One-time preparation of the last two years of Research reports.

Run from the project root:
    python -m app.pipeline.backfill_research
    python -m app.pipeline.backfill_research --include-adhoc
    python -m app.pipeline.backfill_research TCS INFY

This processes annual reports for the two most recent reporting years
and concall transcripts within the past two years. Completed documents
are persisted individually, so rerunning resumes without re-embedding.
"""

import argparse
import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from app.config import DATA_DIR, EMBEDDING_MODEL, OPENAI_API_KEY, load_portfolio
from app.db import init_db
from app.pipeline.pull_pipeline import sync_ticker
from app.storage import atomic_write


def select_tickers(tickers: list[str], include_adhoc: bool = False) -> list[str]:
    if not tickers:
        portfolio = load_portfolio()
        companies = portfolio["portfolio"] + (portfolio.get("adhoc", []) if include_adhoc else [])
        tickers = [company["ticker"] for company in companies]
    normalized = list(dict.fromkeys(ticker.strip().upper() for ticker in tickers))
    if not normalized or any(not re.fullmatch(r"[A-Z0-9&\-]{1,20}", ticker) for ticker in normalized):
        raise ValueError("Provide valid NSE ticker symbols.")
    return normalized


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def backfill_dates(as_of: date | None = None) -> tuple[date, date]:
    end = as_of or datetime.now(timezone(timedelta(hours=5, minutes=30))).date()
    try:
        start = end.replace(year=end.year - 2)
    except ValueError:
        start = end.replace(year=end.year - 2, day=28)
    return start, end


def run_backfill(tickers: list[str], report_path: Path, *, as_of: date | None = None) -> int:
    init_db()
    since, end = backfill_dates(as_of)
    report = {"started_at": _now(), "state": "running", "embedding_model": EMBEDDING_MODEL,
              "tickers": tickers, "current_ticker": None, "results": [], "documents": [],
              "scope": {"years": 2, "since": since.isoformat(), "as_of": end.isoformat(),
                        "annual_report_years": list(range(since.year + 1, end.year + 1)),
                        "month_only_concalls": "First day of the labelled month is used for filtering."}}

    def save():
        report["updated_at"] = _now()
        atomic_write(report_path, json.dumps(report, indent=2).encode("utf-8"))

    def progress(event):
        report["documents"].append(event)
        save()
        suffix = f" (+{event['chunks_added']} chunks)" if event["status"] == "indexed" else ""
        print(f"  [{event['position']}/{event['total']}] {event['label']}: {event['status']}{suffix}", flush=True)

    save()
    try:
        for position, ticker in enumerate(tickers, start=1):
            report["current_ticker"] = ticker
            save()
            print(f"\n[{position}/{len(tickers)}] {ticker}: checking the last two years of reports...", flush=True)
            try:
                result = sync_ticker(ticker, all_reports=True, progress=progress, since=since, as_of=end)
            except Exception:
                result = {"ticker": ticker, "checked": 0, "indexed": 0, "unchanged": 0,
                          "chunks_added": 0, "failed": ["Company discovery or sync failed."]}
            if not result["checked"] and not result["failed"]:
                result["failed"] = ["No annual report or transcript links were found in the selected window."]
            report["results"].append(result)
            save()
            print(f"  {ticker}: {result['indexed']} indexed, {result['unchanged']} reused, "
                  f"{result.get('skipped_outside_window', 0)} outside the window, {len(result['failed'])} failed.", flush=True)
    except KeyboardInterrupt:
        report["state"] = "interrupted"
        save()
        print(f"\nStopped. Completed reports are saved; rerun this command to resume.\nReport: {report_path}", flush=True)
        return 130

    failed = [result["ticker"] for result in report["results"] if result["failed"]]
    report.update(state="partial" if failed else "complete", finished_at=_now(), current_ticker=None,
                  totals={"companies": len(tickers), "checked": sum(r["checked"] for r in report["results"]),
                          "indexed": sum(r["indexed"] for r in report["results"]),
                          "unchanged": sum(r["unchanged"] for r in report["results"]),
                          "chunks_added": sum(r["chunks_added"] for r in report["results"]),
                          "skipped_outside_window": sum(r.get("skipped_outside_window", 0) for r in report["results"]),
                          "failed_companies": failed})
    save()
    totals = report["totals"]
    print(f"\nBackfill {report['state']}: {totals['checked']} reports checked, {totals['indexed']} indexed, "
          f"{totals['unchanged']} reused.\nReport: {report_path}", flush=True)
    if failed:
        print(f"Retry needed for: {', '.join(failed)}. Rerun with these tickers; completed vectors are reused.", flush=True)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Download and index the last two years of Research reports for your portfolio.")
    parser.add_argument("tickers", nargs="*", help="Optional ticker subset; defaults to Your portfolio in seed_data/portfolio.json.")
    parser.add_argument("--include-adhoc", action="store_true", help="Also include the ad-hoc watchlist when no tickers are specified.")
    parser.add_argument("--dry-run", action="store_true", help="List the selected tickers and output path without network or model calls.")
    parser.add_argument("--report", type=Path, default=DATA_DIR / "research_backfill_report.json", help="JSON progress/result file.")
    args = parser.parse_args(argv)
    try:
        tickers = select_tickers(args.tickers, args.include_adhoc)
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Companies ({len(tickers)}): {', '.join(tickers)}", flush=True)
    since, end = backfill_dates()
    print(f"Scope: last 2 years ({since.isoformat()} through {end.isoformat()}); "
          f"annual report years {since.year + 1} and {end.year}. Embedding model: {EMBEDDING_MODEL}", flush=True)
    print(f"Progress report: {args.report.resolve()}", flush=True)
    if args.dry_run:
        return 0
    if not OPENAI_API_KEY:
        parser.error("Set OPENAI_API_KEY in .env before indexing reports.")
    return run_backfill(tickers, args.report.resolve(), as_of=end)


if __name__ == "__main__":
    raise SystemExit(main())
