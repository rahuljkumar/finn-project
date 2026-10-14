import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from app.db import init_db
from app.pipeline import backfill_research as backfill, pull_pipeline as pull


PORTFOLIO = {"portfolio": [{"ticker": "AAA"}, {"ticker": "BBB"}], "adhoc": [{"ticker": "CCC"}]}
DOCS = {"annual_reports": [{"year": year, "url": f"https://example.com/annual-{year}.pdf"}
                           for year in (2026, 2025)],
        "concalls": [{"label": f"Call {number}", "transcript_url": f"https://example.com/call-{number}.pdf"}
                     for number in range(10)]}


class BackfillTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        for target, value in (("app.db.DB_PATH", str(self.folder / "test.db")),
                              ("app.pipeline.pull_pipeline.INDEX_DIR", self.folder / "indexes")):
            test_patch = patch(target, value)
            test_patch.start()
            self.addCleanup(test_patch.stop)
        init_db()

    @staticmethod
    def document(url):
        return {"content_hash": url, "pages": [{"page": 1, "text": f"Report evidence for {url}"}]}

    def test_full_backfill_includes_older_reports_and_resumes_without_reembedding(self):
        events = []
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=DOCS), \
             patch.object(pull, "fetch_document", side_effect=self.document) as fetch, \
             patch.object(pull, "download_pdf", return_value=b"%PDF-saved") as download, \
             patch.object(pull, "embed", side_effect=lambda texts: [[1, 0] for _ in texts]) as embed:
            recent = pull.sync_ticker("AAA")
            self.assertEqual(recent["checked"], 9)
            fetch.reset_mock()
            embed.reset_mock()
            full = pull.sync_ticker("AAA", all_reports=True, progress=events.append)
            self.assertEqual(full["checked"], 12)
            self.assertEqual(full["indexed"], 3)
            self.assertEqual(full["unchanged"], 9)
            self.assertEqual(fetch.call_count, 3)
            self.assertEqual(embed.call_count, 3)
            self.assertEqual(download.call_count, 9)
            self.assertEqual(len(events), 12)
            fetch.reset_mock()
            embed.reset_mock()
            download.reset_mock()
            resumed = pull.sync_ticker("AAA", all_reports=True)
            self.assertEqual(resumed["unchanged"], 12)
            self.assertEqual(resumed["chunks_added"], 0)
            fetch.assert_not_called()
            embed.assert_not_called()
            self.assertEqual(download.call_count, 12)
        self.assertEqual(len(pull.load_index("AAA")), 12)

    def test_one_failed_report_does_not_stop_backfill_or_remove_saved_reports(self):
        docs = {"annual_reports": DOCS["annual_reports"], "concalls": DOCS["concalls"][:1]}
        failed_url = docs["annual_reports"][1]["url"]
        events = []
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=docs), \
             patch.object(pull, "fetch_document", side_effect=lambda url: None if url == failed_url else self.document(url)), \
             patch.object(pull, "embed", side_effect=lambda texts: [[1, 0] for _ in texts]), \
             self.assertLogs(pull.logger, level="ERROR"):
            result = pull.sync_ticker("AAA", all_reports=True, progress=events.append)
        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["failed"], ["Annual Report 2025"])
        self.assertEqual([event["status"] for event in events], ["indexed", "failed", "indexed"])
        self.assertEqual(len(pull.load_index("AAA")), 2)

    def test_full_backfill_ensures_raw_pdfs_even_when_vectors_already_exist(self):
        original = [{"label": "Annual Report 2026", "url": DOCS["annual_reports"][0]["url"],
                     "text": "Previously indexed report", "embedding": [1, 0]}]
        pull.INDEX_DIR.mkdir()
        (pull.INDEX_DIR / "AAA.json").write_text(json.dumps(original), encoding="utf-8")
        docs = {"annual_reports": DOCS["annual_reports"][:1], "concalls": []}
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=docs), \
             patch.object(pull, "download_pdf", return_value=None) as download, \
             patch.object(pull, "embed") as embed, self.assertLogs(pull.logger, level="ERROR"):
            result = pull.sync_ticker("AAA", all_reports=True)
        download.assert_called_once_with(original[0]["url"])
        embed.assert_not_called()
        self.assertEqual(result["failed"], ["Annual Report 2026"])
        self.assertEqual(pull.load_index("AAA"), original)

    def test_duplicate_discovery_links_are_processed_once(self):
        url = DOCS["annual_reports"][0]["url"]
        docs = {"annual_reports": [{"year": year, "url": url} for year in (2026, 2025)], "concalls": []}
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=docs), \
             patch.object(pull, "fetch_document", side_effect=self.document) as fetch, \
             patch.object(pull, "embed", side_effect=lambda texts: [[1, 0] for _ in texts]):
            result = pull.sync_ticker("AAA", all_reports=True)
        self.assertEqual(result["checked"], 1)
        self.assertEqual(result["indexed"], 1)
        fetch.assert_called_once_with(url)
        self.assertEqual(pull.load_index("AAA")[0]["label"], "Annual Report 2026")

    def test_two_year_window_filters_before_downloads_and_retains_stored_history(self):
        docs = {"annual_reports": [{"year": year, "url": f"https://example.com/ar-{year}.pdf"}
                                   for year in (2026, 2025, 2024, 2027)],
                "concalls": [{"label": label, "transcript_url": f"https://example.com/{label.replace(' ', '-')}.pdf"}
                             for label in ("Oct 2026", "Jan 2025", "Nov 2024", "Oct 2024", "Jul 2023", "Nov 2026", "Undated")]}
        saved = {"label": "Annual Report 2023", "url": "https://example.com/saved.pdf",
                 "text": "Previously stored evidence", "embedding": [1, 0]}
        pull.INDEX_DIR.mkdir()
        (pull.INDEX_DIR / "AAA.json").write_text(json.dumps([saved]), encoding="utf-8")
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=docs), \
             patch.object(pull, "fetch_document", side_effect=self.document) as fetch, \
             patch.object(pull, "download_pdf") as download, \
             patch.object(pull, "embed", side_effect=lambda texts: [[1, 0] for _ in texts]) as embed:
            result = pull.sync_ticker("AAA", all_reports=True, since=date(2024, 10, 14), as_of=date(2026, 10, 14))
        urls = {call.args[0] for call in fetch.call_args_list}
        self.assertEqual(urls, {"https://example.com/ar-2026.pdf", "https://example.com/ar-2025.pdf",
                                "https://example.com/Oct-2026.pdf", "https://example.com/Jan-2025.pdf",
                                "https://example.com/Nov-2024.pdf"})
        download.assert_not_called()
        self.assertEqual(embed.call_count, 5)
        self.assertEqual(result["skipped_outside_window"], 5)
        self.assertEqual(result["failed"], ["Date unavailable: Concall Undated"])
        self.assertEqual(result["undated"][0]["url"], "https://example.com/Undated.pdf")
        self.assertEqual(pull.load_index("AAA")[0], saved)

    def test_precise_concall_dates_respect_both_window_boundaries(self):
        docs = {"annual_reports": [], "concalls": [
            {"label": label, "transcript_url": f"https://example.com/{number}.pdf"}
            for number, label in enumerate(("13 Oct 2024", "14 Oct 2024", "14 Oct 2026", "15 Oct 2026"))]}
        with patch.object(pull, "fetch_soup", return_value=BeautifulSoup("<html/>", "html.parser")), \
             patch.object(pull, "get_documents", return_value=docs), \
             patch.object(pull, "fetch_document", side_effect=self.document) as fetch, \
             patch.object(pull, "embed", side_effect=lambda texts: [[1, 0] for _ in texts]):
            result = pull.sync_ticker("AAA", all_reports=True, since=date(2024, 10, 14), as_of=date(2026, 10, 14))
        self.assertEqual([call.args[0] for call in fetch.call_args_list], ["https://example.com/1.pdf", "https://example.com/2.pdf"])
        self.assertEqual(result["indexed"], 2)
        self.assertEqual(result["skipped_outside_window"], 2)

    def test_date_range_handles_leap_day(self):
        self.assertEqual(backfill.backfill_dates(date(2024, 2, 29)), (date(2022, 2, 28), date(2024, 2, 29)))

    def test_selection_defaults_to_portfolio_and_watchlist_is_optional(self):
        with patch.object(backfill, "load_portfolio", return_value=PORTFOLIO):
            self.assertEqual(backfill.select_tickers([]), ["AAA", "BBB"])
            self.assertEqual(backfill.select_tickers([], include_adhoc=True), ["AAA", "BBB", "CCC"])
            self.assertEqual(backfill.select_tickers([" aaa ", "AAA", "bbb"]), ["AAA", "BBB"])
            with self.assertRaises(ValueError):
                backfill.select_tickers(["../outside"])

    def test_dry_run_requires_no_key_and_does_no_work(self):
        report = self.folder / "dry-run.json"
        with patch.object(backfill, "load_portfolio", return_value=PORTFOLIO), \
             patch.object(backfill, "OPENAI_API_KEY", ""), patch.object(backfill, "run_backfill") as run, \
             redirect_stdout(io.StringIO()):
            code = backfill.main(["--dry-run", "--report", str(report)])
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertFalse(report.exists())

    def test_missing_key_is_caught_before_downloading_anything(self):
        with patch.object(backfill, "OPENAI_API_KEY", ""), patch.object(backfill, "run_backfill") as run, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                backfill.main(["AAA"])
        self.assertEqual(error.exception.code, 2)
        run.assert_not_called()

    def test_failed_company_is_reported_and_other_companies_continue(self):
        report_path = self.folder / "results.json"

        def sync(ticker, *, all_reports, progress, since, as_of):
            self.assertTrue(all_reports)
            self.assertEqual((since, as_of), (date(2024, 10, 14), date(2026, 10, 14)))
            if ticker == "BBB":
                raise RuntimeError("Discovery unavailable")
            progress({"ticker": ticker, "label": "Annual Report", "url": "https://example.com/a.pdf",
                      "status": "indexed", "position": 1, "total": 1, "chunks_added": 2})
            return {"ticker": ticker, "checked": 1, "indexed": 1, "unchanged": 0, "chunks_added": 2, "failed": []}

        with patch.object(backfill, "sync_ticker", side_effect=sync) as work, redirect_stdout(io.StringIO()):
            code = backfill.run_backfill(["AAA", "BBB", "CCC"], report_path, as_of=date(2026, 10, 14))
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertEqual(work.call_count, 3)
        self.assertEqual(report["state"], "partial")
        self.assertEqual(report["scope"]["annual_report_years"], [2025, 2026])
        self.assertEqual(report["totals"]["indexed"], 2)
        self.assertEqual(report["totals"]["failed_companies"], ["BBB"])
        self.assertEqual([event["ticker"] for event in report["documents"]], ["AAA", "CCC"])

    def test_interrupt_preserves_completed_report_progress(self):
        report_path = self.folder / "interrupted.json"

        def interrupted(ticker, *, all_reports, progress, since, as_of):
            progress({"ticker": ticker, "label": "Annual Report", "status": "indexed",
                      "position": 1, "total": 2, "chunks_added": 2})
            raise KeyboardInterrupt()

        with patch.object(backfill, "sync_ticker", side_effect=interrupted), redirect_stdout(io.StringIO()):
            code = backfill.run_backfill(["AAA"], report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 130)
        self.assertEqual(report["state"], "interrupted")
        self.assertEqual(len(report["documents"]), 1)
        self.assertEqual(report["current_ticker"], "AAA")

    def test_empty_discovery_is_not_reported_as_demo_ready(self):
        result = {"ticker": "AAA", "checked": 0, "indexed": 0, "unchanged": 0, "chunks_added": 0, "failed": []}
        report_path = self.folder / "empty.json"
        with patch.object(backfill, "sync_ticker", return_value=result), redirect_stdout(io.StringIO()):
            code = backfill.run_backfill(["AAA"], report_path)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(report_path.read_text())["totals"]["failed_companies"], ["AAA"])


if __name__ == "__main__":
    unittest.main()
