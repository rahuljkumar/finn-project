import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import feeds
from app.db import get_conn, init_db
from app.main import app
from app.pipeline import digest, promised_vs_delivered as pvd
from app.pipeline.research_cache import cached_research
from app.sources import nse_client, nse_reports
from app.sources.nse_rss import parse_announcements
from app.storage import atomic_write

COMPANIES = [{"ticker": "AAA", "name": "Alpha"}, {"ticker": "BBB", "name": "Beta"}]
PORTFOLIO = {"portfolio": COMPANIES, "adhoc": []}
CSV = b"SYMBOL, SERIES, DATE1, PREV_CLOSE, CLOSE_PRICE, TTL_TRD_QNTY\nAAA, EQ, 28-Sep-2026, 100, 101, 300\nAAA, BE, 28-Sep-2026, 100, 999, 900\nBBB, EQ, 28-Sep-2026, 100, 99, 400\n"


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        db_patch = patch("app.db.DB_PATH", str(Path(self.folder.name) / "test.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        init_db()

    def test_official_report_filters_series_and_uses_reported_previous_close(self):
        rows = nse_reports.parse_report(CSV, date(2026, 9, 28), {"AAA", "BBB"})
        self.assertEqual(rows["AAA"]["Close"], 101)
        self.assertAlmostEqual(rows["AAA"]["pct_change"], 1)
        self.assertAlmostEqual(rows["BBB"]["pct_change"], -1)
        with self.assertRaises(ValueError):
            nse_reports.parse_report(CSV, date(2026, 9, 25), {"AAA"})
        with self.assertRaises(ValueError):
            nse_reports.parse_report(b"<html>Blocked</html>", date(2026, 9, 28), {"AAA"})

    def test_report_backfill_builds_full_20_session_average_and_includes_weekends(self):
        today = date(2026, 9, 28)
        days = [today - timedelta(days=i) for i in range(20)]
        def report(day, tickers, revalidate=False):
            return {"AAA": {"Date": nse_reports.pd.Timestamp(day), "Close": 100,
                            "Volume": 100 + days.index(day), "pct_change": 0.5}} if day in days else {}
        with patch.object(nse_reports, "_report", side_effect=report):
            history = nse_reports.fetch_portfolio_history(["AAA"], today=today)["AAA"]
        self.assertEqual(len(history), 20)
        self.assertEqual(history.iloc[-1]["avg_volume_20d"], 109.5)
        self.assertEqual(history.iloc[-1]["Date"].date(), today)

    def test_blocked_archive_stops_before_full_backfill(self):
        with patch.object(nse_reports, "_report", side_effect=ValueError("Blocked")) as fetch:
            self.assertEqual(nse_reports.fetch_portfolio_history(["AAA"], today=date(2026, 9, 28)), {})
        self.assertEqual(fetch.call_count, 5)

    def test_cached_report_does_not_mask_an_upstream_outage(self):
        folder = Path(self.folder.name) / "reports"
        folder.mkdir()
        (folder / "2026-09-28.csv").write_bytes(CSV)
        with patch.object(nse_reports, "REPORT_DIR", folder), \
             patch.object(nse_reports.requests, "get", side_effect=nse_reports.requests.RequestException("Blocked")):
            self.assertEqual(nse_reports._report(date(2026, 9, 28), {"AAA"})["AAA"]["Close"], 101)
            self.assertEqual(nse_reports.fetch_portfolio_history(["AAA"], today=date(2026, 9, 28)), {})

    def test_historical_api_stops_on_first_block_instead_of_hitting_each_stock(self):
        from unittest.mock import Mock
        session = Mock()
        session.get.return_value.raise_for_status.side_effect = nse_client.requests.HTTPError("403")
        with patch.object(nse_client, "_session", return_value=session), \
             self.assertLogs("app.sources.nse_client", level="WARNING"):
            self.assertEqual(nse_client.fetch_announcements(["AAA", "BBB"]), [])
        self.assertEqual(session.get.call_count, 1)

    def test_rss_matches_exact_companies_or_ticker_filename_and_keeps_links(self):
        xml = b'''<rss><channel><item><title>Alpha Limited</title><link>https://nsearchives.nseindia.com/corporate/xbrl/file.xml</link><description>Alpha changed |SUBJECT: Acquisition</description><pubDate>28-Sep-2026 15:30:00</pubDate></item><item><title>Beta Industries Limited</title><link>https://nsearchives.nseindia.com/corporate/BBB_announcement.pdf</link><description>Results |SUBJECT: Integrated Filing- Financial</description><pubDate>28-Sep-2026 15:00:00</pubDate></item><item><title>Alpha Beta Limited</title><link>https://example.com/unrelated.pdf</link></item></channel></rss>'''
        rows = parse_announcements(xml, COMPANIES)
        self.assertEqual([r["symbol"] for r in rows], ["AAA", "BBB"])
        self.assertEqual(rows[0]["desc"], "Acquisition")
        self.assertTrue(rows[1]["attchmntFile"].endswith(".pdf"))
        self.assertEqual(rows, parse_announcements(xml, COMPANIES))

    def test_nse_times_are_converted_from_ist_to_utc(self):
        self.assertEqual(digest._parse_nse_dt("28-Sep-2026 15:30:00"), "2026-09-28T10:00:00+00:00")

    def test_old_nse_timestamps_are_migrated_only_once(self):
        with get_conn() as conn:
            conn.execute("DELETE FROM migrations WHERE name='nse_ist'")
            conn.execute("INSERT INTO announcements (id, source, published_at, ticker) VALUES ('old', 'nse_live', '2026-09-28T15:30:00+00:00', 'AAA')")
        init_db()
        init_db()
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT published_at FROM announcements WHERE id='old'").fetchone()[0], "2026-09-28T10:00:00+00:00")

    def test_rss_refresh_deduplicates_without_reclassifying_saved_filings(self):
        item = {"symbol": "AAA", "seq_id": "rss-id", "an_dt": "28-Sep-2026 15:30:00",
                "desc": "Acquisition", "attchmntText": "Alpha acquisition", "attchmntFile": "https://example.com/filing.pdf"}
        with patch.object(digest, "load_portfolio", return_value=PORTFOLIO), \
             patch.object(digest, "USE_LIVE_NSE", True), \
             patch.object(digest, "fetch_recent_announcements", return_value=[item]), \
             patch.object(digest, "fetch_announcements", return_value=[]), \
             patch.object(digest, "classify_batch", wraps=digest.classify_batch) as classify:
            self.assertEqual(digest.refresh_all(), 1)
            self.assertEqual(digest.refresh_all(), 0)
            self.assertEqual(classify.call_count, 1)
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 1)

    def test_live_failure_preserves_rows_and_never_loads_sample_data(self):
        with get_conn() as conn:
            conn.execute("INSERT INTO announcements (id,ticker) VALUES ('saved','AAA')")
        with patch.object(digest, "load_portfolio", return_value=PORTFOLIO), \
             patch.object(digest, "USE_LIVE_NSE", True), \
             patch.object(digest, "fetch_recent_announcements", side_effect=RuntimeError("Offline")), \
             patch.object(digest, "fetch_announcements", return_value=[]), \
             patch.object(digest, "load_seed_announcements") as seed, \
             self.assertLogs("app.pipeline.digest", level="WARNING"):
            with self.assertRaises(RuntimeError):
                digest.refresh_all()
            seed.assert_not_called()
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 1)

    def test_same_filing_from_rss_and_api_is_inserted_once(self):
        item = {"symbol": "AAA", "an_dt": "28-Sep-2026 15:30:00", "desc": "Acquisition",
                "attchmntText": "Alpha acquired", "attchmntFile": "https://example.com/filing.pdf"}
        with patch.object(digest, "load_portfolio", return_value=PORTFOLIO), \
             patch.object(digest, "USE_LIVE_NSE", True), \
             patch.object(digest, "fetch_recent_announcements", return_value=[{**item, "desc": "Acquisition-XBRL"}]), \
             patch.object(digest, "fetch_announcements", return_value=[item]):
            self.assertEqual(digest.refresh_all(), 1)
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM announcements").fetchone()[0], 1)

    def test_short_api_fallback_does_not_claim_complete_historical_backfill(self):
        item = {"symbol": "AAA", "an_dt": "28-Sep-2026 15:30:00", "desc": "Acquisition",
                "attchmntText": "Alpha acquired", "attchmntFile": "https://example.com/filing.pdf"}
        with get_conn() as conn:
            conn.execute("INSERT INTO feed_state (feed,attempted_at) VALUES ('filings_backfill',?)",
                         (datetime.now(timezone.utc).isoformat(),))
        with patch.object(digest, "load_portfolio", return_value=PORTFOLIO), \
             patch.object(digest, "USE_LIVE_NSE", True), \
             patch.object(digest, "fetch_recent_announcements", side_effect=RuntimeError("RSS offline")), \
             patch.object(digest, "fetch_announcements", return_value=[item]) as api, \
             self.assertLogs("app.pipeline.digest", level="WARNING"):
            self.assertEqual(digest.refresh_all(), 1)
        self.assertEqual(api.call_args.kwargs["days"], 2)
        with get_conn() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM migrations WHERE name='nse_backfill'").fetchone())

    def test_feed_failure_retains_success_timestamp_and_existing_summary(self):
        with get_conn() as conn:
            conn.execute("INSERT INTO feed_state (feed,succeeded_at,summary) VALUES ('prices',?,?)",
                         ("2026-09-28T10:00:00+00:00", json.dumps({"refreshed": 25})))
        with patch("app.pipeline.alerts.refresh_price_history", side_effect=RuntimeError("Offline")), \
             self.assertLogs("app.feeds", level="ERROR"):
            feeds._job("prices")
        status = feeds.feed_status("prices")
        self.assertEqual(status["succeeded_at"], "2026-09-28T10:00:00+00:00")
        self.assertEqual(status["summary"]["refreshed"], 25)
        self.assertIn("unavailable", status["error"])

    def test_repeated_refresh_clicks_share_one_background_job(self):
        started, finish, finished = threading.Event(), threading.Event(), threading.Event()
        def job(feed):
            started.set()
            finish.wait(3)
            with feeds._guard:
                feeds._running.discard(feed)
            finished.set()
        with patch.object(feeds, "_job", side_effect=job) as worker:
            try:
                self.assertTrue(feeds.request_refresh("prices", force=True))
                self.assertTrue(started.wait(1))
                self.assertFalse(feeds.request_refresh("prices", force=True))
                self.assertEqual(worker.call_count, 1)
            finally:
                finish.set()
                self.assertTrue(finished.wait(2))
            self.assertFalse(feeds.request_refresh("prices", force=True))

    def test_page_returns_saved_results_while_background_refresh_runs(self):
        with feeds._guard:
            feeds._running.add("filings")
        try:
            response = TestClient(app).get("/digest?hours=168")
        finally:
            with feeds._guard:
                feeds._running.discard("filings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Refreshing data", response.text)
        self.assertIn('hx-trigger="every 5s"', response.text)
        self.assertIn('hx-get="/digest?hours=168"', response.text)

    def test_research_cache_survives_restart_and_falls_back_during_outage(self):
        data = {"content": "**Revenue** grew.", "sources": [{"label": "Annual Report", "url": "https://example.com/report.pdf"}]}
        first = cached_research("AAA", "business_snapshot", lambda: data)
        def fail():
            raise RuntimeError("Offline")
        self.assertEqual(cached_research("AAA", "business_snapshot", fail), first)
        # Reopening the database does not discard research answers.
        init_db()
        with patch("app.pipeline.research_cache.FRESH_FOR", timedelta(0)), \
             self.assertLogs("app.pipeline.research_cache", level="WARNING"):
            saved = cached_research("AAA", "business_snapshot", fail)
        self.assertTrue(saved["stale"])
        self.assertEqual(saved["data"], data)
        self.assertEqual(saved["saved_at"], first["saved_at"])

    def test_failed_research_is_not_saved_as_a_successful_answer(self):
        cached_research("AAA", "business_snapshot", lambda: {"content": "Couldn't generate this section right now."})
        with get_conn() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM research_cache").fetchone()[0], 0)

    def test_atomic_write_failure_preserves_previous_cache(self):
        path = Path(self.folder.name) / "cache.json"
        path.write_bytes(b"previous")
        with patch("app.storage.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_write(path, b"new value")
        self.assertEqual(path.read_bytes(), b"previous")
        self.assertEqual(list(path.parent.glob(".cache.json.*")), [])

    def test_promised_vs_delivered_retrieves_the_selected_concall_label(self):
        table = {"Mar 2026": {m: 100 for m in ("revenue", "ebitda", "pbt", "pat")},
                 "Jun 2026": {m: 110 for m in ("revenue", "ebitda", "pbt", "pat")}}
        chunks = [{"label": "Concall Apr 2026", "text": "We guide to 10% growth", "embedding": [1, 0]},
                  {"label": "Concall Jul 2026", "text": "Future unrelated guidance", "embedding": [1, 0]}]
        with patch.object(pvd, "fetch_soup", return_value=object()), \
             patch.object(pvd, "get_quarterly_table", return_value=table), \
             patch.object(pvd, "get_documents", return_value={"concalls": [{"label": "Apr 2026", "transcript_url": "https://example.com/apr.pdf"}]}), \
             patch.object(pvd, "build_index", return_value=chunks), \
             patch.object(pvd, "embed", return_value=[[1, 0]]), \
             patch.object(pvd, "chat_json", return_value={"verdict": "met", "rationale": "Guided growth delivered"}) as judge:
            result = pvd.build_promised_vs_delivered("AAA", n_quarters=1)
        self.assertIn("We guide to 10% growth", judge.call_args.args[0])
        self.assertNotIn("Future unrelated guidance", judge.call_args.args[0])
        self.assertEqual(result[0]["guidance_source"], "Apr 2026")


if __name__ == "__main__":
    unittest.main()
