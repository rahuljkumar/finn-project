import io
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import jobs
from app.config import load_portfolio
from app.db import get_conn, init_db
from app.pipeline import prepare_research as prepare


class ResearchPrecomputeTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.revision = "evidence-a"
        for target, value in (
            ("app.db.DB_PATH", str(Path(folder.name) / "research.db")),
            ("app.jobs.OPENAI_API_KEY", "test-key-no-network"),
        ):
            mocked = patch(target, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        init_db()
        for target, kwargs in (
            ("app.pipeline.prepare_research.corpus_revision", {"side_effect": lambda _: self.revision}),
            ("app.pipeline.pull_pipeline.corpus_revision", {"side_effect": lambda _: self.revision}),
            ("app.pipeline.prepare_research.document_status", {"return_value": {"ready": True}}),
            ("app.pipeline.prepare_research.get_snapshot", {"return_value": {}}),
        ):
            mocked = patch(target, **kwargs)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.generated = []

        def generate(ticker, category):
            self.generated.append((ticker, category))
            if category == "promised_vs_delivered":
                return []
            return {"content": f"Saved {ticker}/{category}", "sources": []}

        mocked = patch.object(prepare, "generate_research_section", side_effect=generate)
        self.generate = mocked.start()
        self.addCleanup(mocked.stop)

    def test_default_portfolio_prepares_twenty_stocks_and_all_nine_topics(self):
        portfolio = load_portfolio()
        summary = prepare.prepare_portfolio()
        self.assertEqual(summary["state"], "complete")
        self.assertEqual((summary["companies"], summary["total"], summary["ready"]), (20, 180, 180))
        self.assertEqual(len(prepare.RESEARCH_CATEGORIES), 9)
        self.assertEqual({ticker for ticker, _ in self.generated},
                         {company["ticker"] for company in portfolio["portfolio"]})
        self.assertFalse({ticker for ticker, _ in self.generated} &
                         {company["ticker"] for company in portfolio["adhoc"]})
        for company in portfolio["portfolio"]:
            self.assertEqual({category for ticker, category in self.generated if ticker == company["ticker"]},
                             set(prepare.RESEARCH_CATEGORIES))
        status = prepare.portfolio_preparation_status()
        self.assertEqual((status["total"], status["ready"], status["companies"], status["state"]),
                         (180, 180, 20, "complete"))

    def test_successful_sections_are_reused_without_model_calls_or_time_expiry(self):
        first = prepare.prepare_company(" aaa ")
        self.assertEqual(first["generated"], 9)
        with get_conn() as conn:
            conn.execute("UPDATE research_cache SET saved_at='2020-01-01T00:00:00+00:00'")
        second = prepare.prepare_company("AAA")
        self.assertEqual((second["ready"], second["reused"], second["generated"]), (9, 9, 0))
        self.assertEqual(self.generate.call_count, 9)
        with patch.object(prepare, "start_job") as start:
            self.assertIsNone(prepare.request_research_preparation("AAA"))
        start.assert_not_called()

    def test_changed_evidence_refreshes_all_sections_then_reuses_new_results(self):
        prepare.prepare_company("AAA")
        prepare.prepare_company("BBB")
        self.revision = "evidence-b"
        changed = prepare.prepare_company("AAA")
        self.assertEqual((changed["generated"], changed["ready"], changed["state"]), (9, 9, "complete"))
        self.assertEqual(self.generate.call_count, 27)
        self.assertEqual(prepare.prepare_company("AAA")["reused"], 9)

    def test_failed_section_retries_without_regenerating_eight_successes(self):
        original = self.generate.side_effect

        def fail_once(ticker, category):
            if category == "guidance":
                raise RuntimeError("Model unavailable")
            return original(ticker, category)

        self.generate.side_effect = fail_once
        with self.assertLogs(prepare.logger, level="ERROR"):
            partial = prepare.prepare_company("AAA")
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["ready"], 8)
        self.assertEqual(partial["pending"], ["guidance"])
        self.assertEqual(partial["failed"], ["guidance"])
        self.generate.side_effect = original
        self.generate.reset_mock()
        complete = prepare.prepare_company("AAA")
        self.generate.assert_called_once_with("AAA", "guidance")
        self.assertEqual((complete["state"], complete["generated"], complete["reused"]), ("complete", 1, 8))

    def test_unsaved_or_stale_generation_is_not_counted_as_ready(self):
        original = self.generate.side_effect
        self.generate.side_effect = lambda ticker, category: (
            {"content": "No primary-source documents available", "sources": []}
            if category == "guidance" else original(ticker, category))
        partial = prepare.prepare_company("AAA")
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["pending"], ["guidance"])
        self.assertEqual(partial["ready"], 8)

    def test_evidence_change_during_generation_leaves_job_incomplete(self):
        original = self.generate.side_effect

        def changed(ticker, category):
            self.revision = "evidence-b"
            return original(ticker, category)

        self.generate.side_effect = changed
        with self.assertLogs(prepare.logger, level="ERROR"):
            partial = prepare.prepare_company("AAA")
        self.assertEqual(partial["state"], "partial")
        self.assertEqual(partial["revision"], "evidence-b")
        self.assertGreater(len(partial["pending"]), 0)
        self.assertEqual(self.generate.call_count, 1)

    def test_missing_sources_do_not_queue_or_call_model(self):
        with patch.object(prepare, "document_status", return_value={"ready": False}), \
             patch.object(prepare, "start_job") as start:
            self.assertIsNone(prepare.request_research_preparation("AAA"))
            result = prepare.prepare_company("AAA")
        start.assert_not_called()
        self.generate.assert_not_called()
        self.assertEqual((result["state"], result["ready"]), ("waiting_sources", 0))

    def test_structured_financial_evidence_is_sufficient_to_queue_without_report_index(self):
        with patch.object(prepare, "document_status", return_value={"ready": False}), \
             patch.object(prepare, "get_snapshot", return_value={"profit_loss": {"Sales": [100]}}), \
             patch.object(prepare, "start_job", return_value={"state": "running"}) as start:
            self.assertEqual(prepare.request_research_preparation("AAA")["state"], "running")
        self.assertEqual(start.call_args.args[:2], (prepare.research_job_key("AAA", "evidence-a"), "research"))
        self.assertIs(start.call_args.kwargs["executor"], prepare._research_pool)

    def test_visitors_share_one_job_and_partial_jobs_back_off_then_resume(self):
        entered, release = threading.Event(), threading.Event()
        original = self.generate.side_effect

        def blocked(ticker, category):
            if category == prepare.RESEARCH_CATEGORIES[0]:
                entered.set()
                release.wait(5)
            if category == "guidance":
                return {"content": "No primary-source documents available", "sources": []}
            return original(ticker, category)

        self.generate.side_effect = blocked
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(prepare, "_research_pool", pool):
            try:
                first = prepare.request_research_preparation("AAA")
                self.assertTrue(entered.wait(2))
                second = prepare.request_research_preparation("AAA")
                self.assertEqual(first["job_key"], second["job_key"])
                self.assertEqual(second["state"], "running")
            finally:
                release.set()
            pool.shutdown(wait=True)
        self.assertEqual(self.generate.call_count, 9)
        cached = prepare.request_research_preparation("AAA")
        self.assertEqual(cached["result"]["state"], "partial")
        self.assertEqual(self.generate.call_count, 9)

        with get_conn() as conn:
            conn.execute("UPDATE ai_jobs SET updated_at=? WHERE job_key=?",
                         ((datetime.now(timezone.utc) - timedelta(seconds=prepare.RETRY_AFTER + 1)).isoformat(),
                          first["job_key"]))
        self.generate.side_effect = original
        self.generate.reset_mock()
        with ThreadPoolExecutor(max_workers=1) as pool, patch.object(prepare, "_research_pool", pool):
            prepare.request_research_preparation("AAA")
        self.generate.assert_called_once_with("AAA", "guidance")
        self.assertEqual(jobs.get_job(first["job_key"])["result"]["state"], "complete")

    def test_model_change_uses_a_new_job_instead_of_old_completed_job(self):
        prepare.prepare_company("AAA")
        old_key = prepare.research_job_key("AAA")
        with get_conn() as conn:
            conn.execute("INSERT INTO ai_jobs VALUES (?, 'research', 'ready', ?, NULL, ?)",
                         (old_key, '{"state":"complete"}', datetime.now(timezone.utc).isoformat()))
        with patch.object(prepare, "REASONING_MODEL", "new-research-model"), \
             patch("app.pipeline.research_cache.REASONING_MODEL", "new-research-model"), \
             patch.object(prepare, "start_job", return_value={"state": "running"}) as start:
            result = prepare.request_research_preparation("AAA")
        self.assertEqual(result["state"], "running")
        self.assertNotEqual(start.call_args.args[0], old_key)
        self.assertIn("new-research-model", start.call_args.args[0])

    def test_portfolio_status_is_read_only_counts_current_sections_and_running_partial_jobs(self):
        prepare.prepare_company("AAA")
        original = self.generate.side_effect
        self.generate.side_effect = lambda ticker, category: (
            {"content": "No primary-source documents available", "sources": []}
            if category == "guidance" else original(ticker, category))
        prepare.prepare_company("BBB")
        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute("INSERT INTO ai_jobs VALUES (?, 'research', 'ready', ?, NULL, ?)",
                         (prepare.research_job_key("BBB"), '{"state":"partial"}', now))
            conn.execute("INSERT INTO ai_jobs VALUES (?, 'research', 'running', NULL, NULL, ?)",
                         (prepare.research_job_key("CCC"), now))
        portfolio = {"portfolio": [{"ticker": ticker} for ticker in ("AAA", "BBB", "CCC")],
                     "adhoc": [{"ticker": "DDD"}]}
        self.generate.reset_mock()
        with patch.object(prepare, "load_portfolio", return_value=portfolio), \
             patch.object(prepare, "start_job") as start, \
             patch("app.pipeline.pull_pipeline.load_index", side_effect=AssertionError("Index must stay unloaded")):
            status = prepare.portfolio_preparation_status()
        self.assertEqual((status["total"], status["ready"], status["pending"]), (27, 17, 10))
        self.assertEqual((status["companies"], status["state"], status["active"], status["failed"]),
                         (3, "preparing", 1, 1))
        start.assert_not_called()
        self.generate.assert_not_called()
        self.generate.side_effect = original
        prepare.prepare_company("BBB")
        prepare.prepare_company("CCC")
        with patch.object(prepare, "load_portfolio", return_value=portfolio):
            complete = prepare.portfolio_preparation_status()
        self.assertEqual((complete["state"], complete["ready"], complete["active"]), ("complete", 27, 0))

    def test_status_with_missing_sources_is_partial_without_triggering_work(self):
        with patch.object(prepare, "start_job") as start:
            status = prepare.portfolio_preparation_status()
        self.assertEqual((status["state"], status["ready"], status["active"]), ("partial", 0, 0))
        start.assert_not_called()
        self.generate.assert_not_called()

    def test_portfolio_queue_excludes_watchlist_and_normalizes_subsets(self):
        with patch.object(prepare, "load_portfolio", return_value={
                "portfolio": [{"ticker": "AAA"}, {"ticker": "BBB"}], "adhoc": [{"ticker": "CCC"}]}), \
             patch.object(prepare, "request_research_preparation", return_value={"state": "running"}) as request:
            self.assertEqual(len(prepare.request_portfolio_preparation()), 2)
        self.assertEqual([call.args[0] for call in request.call_args_list], ["AAA", "BBB"])
        summary = prepare.prepare_portfolio([" aaa ", "AAA", "bbb"])
        self.assertEqual(summary["companies"], 2)
        with self.assertRaises(ValueError):
            prepare.prepare_company("../outside")

    def test_cli_outputs_completion_and_nonzero_for_incomplete_sources(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(prepare.main(["AAA"]), 0)
        self.assertIn("9/9 sections ready", output.getvalue())
        with patch.object(prepare, "document_status", return_value={"ready": False}), \
             redirect_stdout(io.StringIO()):
            self.assertEqual(prepare.main(["BBB"]), 1)


if __name__ == "__main__":
    unittest.main()
