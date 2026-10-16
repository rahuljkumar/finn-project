import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from app import feeds
from app.db import get_conn, init_db
from app.pipeline import pull_pipeline as pull
from test_support import isolate_background


class ResearchSourceTests(unittest.TestCase):
    def setUp(self):
        isolate_background(self)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        for target, value in (("app.db.DB_PATH", str(Path(folder.name) / "research.db")),
                              ("app.pipeline.pull_pipeline.INDEX_DIR", Path(folder.name) / "indexes")):
            setting = patch(target, value)
            setting.start()
            self.addCleanup(setting.stop)
        init_db()
        pull.INDEX_DIR.mkdir()

    def sources(self, ticker):
        (pull.INDEX_DIR / f"{ticker}.json").write_text('[{"text":"Saved evidence","embedding":[1,0]}]')
        with get_conn() as conn:
            conn.execute("INSERT INTO research_snapshots VALUES (?,?,'financials-v1','2026-10-16')", (ticker, '{}'))
            conn.execute("INSERT INTO research_documents VALUES (?,?,'Annual Report 2026','annual_report','hash-v1','indexed','2026-10-16')",
                         (ticker, f"https://example.com/{ticker}.pdf"))

    def test_check_times_failed_redownloads_and_index_touch_do_not_invalidate_evidence(self):
        self.sources("AAA")
        revision = pull.corpus_revision("AAA")
        with get_conn() as conn:
            conn.execute("UPDATE research_documents SET status='error',checked_at='2026-10-17'")
            conn.execute("UPDATE research_snapshots SET saved_at='2026-10-17'")
        index = pull.INDEX_DIR / "AAA.json"
        stamp = index.stat()
        os.utime(index, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
        with patch.object(pull, "load_index", side_effect=AssertionError("Status must not load vectors")):
            self.assertEqual(pull.corpus_revision("AAA"), revision)
            self.assertTrue(pull.document_status("AAA")["ready"])

    def test_report_content_or_financial_change_invalidates_only_affected_company(self):
        self.sources("AAA")
        self.sources("BBB")
        first, other = pull.corpus_revision("AAA"), pull.corpus_revision("BBB")
        with get_conn() as conn:
            conn.execute("UPDATE research_documents SET content_hash='hash-v2' WHERE ticker='AAA'")
        second = pull.corpus_revision("AAA")
        self.assertNotEqual(second, first)
        self.assertEqual(pull.corpus_revision("BBB"), other)
        with get_conn() as conn:
            conn.execute("UPDATE research_snapshots SET revision='financials-v2' WHERE ticker='AAA'")
        self.assertNotEqual(pull.corpus_revision("AAA"), second)
        self.assertEqual(pull.corpus_revision("BBB"), other)

    def test_generation_uses_the_financial_tables_and_ratios_for_each_topic(self):
        snapshot = {"profit_loss": {"revenue": 10}, "balance_sheet": {"debt": 2},
                    "ratios": {"debtor_days": 30}, "cash_flow": {"operating": 3}}
        with patch.object(pull, "get_snapshot", return_value=snapshot), patch.object(pull, "generate_section") as generate:
            pull.generate_research_section("AAA", "balance_sheet")
            self.assertEqual(generate.call_args.kwargs["structured"],
                             {"balance_sheet": snapshot["balance_sheet"], "ratios": snapshot["ratios"]})
            pull.generate_research_section("AAA", "financial_snapshot")
            self.assertEqual(generate.call_args.kwargs["structured"], snapshot["profit_loss"])
            pull.generate_research_section("AAA", "cash_quality")
            self.assertEqual(generate.call_args.kwargs["structured"], snapshot["cash_flow"])
            pull.generate_research_section("AAA", "business_snapshot")
            self.assertIsNone(generate.call_args.kwargs["structured"])
        with patch("app.pipeline.promised_vs_delivered.build_promised_vs_delivered", return_value=[]) as pvd:
            self.assertEqual(pull.generate_research_section("AAA", "promised_vs_delivered"), [])
        pvd.assert_called_once_with("AAA")

    def test_portfolio_sync_completion_queues_its_saved_sections(self):
        portfolio = {"portfolio": [{"ticker": "AAA"}], "adhoc": [{"ticker": "BBB"}]}
        with patch.object(pull, "load_portfolio", return_value=portfolio), \
             patch.object(pull, "_sync_ticker", return_value={"indexed": 1}), \
             patch("app.pipeline.prepare_research.request_research_preparation") as queue:
            self.assertEqual(pull.sync_ticker("AAA", prepare_sections=True), {"indexed": 1})
            pull.sync_ticker("BBB", prepare_sections=True)
            pull.sync_ticker("AAA")  # Standalone ingestion does not launch AI.
        queue.assert_called_once_with("AAA")

    def test_startup_research_feed_only_queues_work(self):
        with get_conn() as conn:
            conn.execute("INSERT INTO feed_state(feed) VALUES('research')")
        with patch("app.pipeline.prepare_research.request_portfolio_preparation", return_value=[
                {"state": "running"}, {"state": "ready"}]) as queue, \
             patch("app.pipeline.prepare_research.prepare_company", side_effect=AssertionError("No generation in scheduler")):
            feeds._job("research")
        queue.assert_called_once_with()
        self.assertEqual(feeds.feed_status("research")["summary"], {"queued": 1})

    def test_restart_resumes_preparation_despite_recent_scheduling_timestamp(self):
        with get_conn() as conn:
            conn.execute("INSERT INTO feed_state(feed,attempted_at) VALUES('research',?)",
                         (datetime.now(timezone.utc).isoformat(),))
        stop = threading.Event()
        with patch("app.pipeline.prepare_research.request_portfolio_preparation", side_effect=stop.set) as queue:
            feeds.poll_feeds(stop)
        queue.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
