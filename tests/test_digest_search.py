import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from fastapi.testclient import TestClient

from app.db import get_conn, init_db
from app import feeds
from app.llm import client as llm
from app.main import app
from app.pipeline import digest
from app.sources import filing_search as search

NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
COMPANIES = [{"ticker": "RELIANCE", "name": "Reliance Industries"}]
PORTFOLIO = {"portfolio": COMPANIES, "adhoc": []}
URL = "https://www.ril.com/investors/disclosures/filing.pdf"
FILING = {"ticker": "RELIANCE", "source_url": URL, "published_date": "2026-09-28",
          "headline": "Reliance Industries announces an acquisition",
          "company_evidence": "Reliance Industries Limited", "date_evidence": "September 28, 2026",
          "content_evidence": "We announce the acquisition of a subsidiary."}
BODY = "Reliance Industries Limited September 28, 2026 We announce the acquisition of a subsidiary."
RESULT = {"filings": [FILING], "sources": [URL], "opened": []}


class DigestSearchTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db_patch = patch("app.db.DB_PATH", str(Path(folder.name) / "test.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        init_db()

    def validate(self, filing=None, sources=None, opened=None, body=BODY):
        result = {"filings": [filing or FILING], "sources": sources if sources is not None else [URL],
                  "opened": opened or []}
        with patch.object(search, "_document_text", return_value=body):
            return search.validate_filings(result, COMPANIES, NOW)

    def test_accepts_original_document_with_company_date_and_content_evidence(self):
        row = self.validate()[0]
        self.assertEqual(row["sort_date"], "2026-09-27T18:30:00+00:00")
        self.assertEqual(row["exchange"], "Company")
        self.assertEqual(row["search_metadata"]["verification"], "document")
        self.assertEqual(len(self.validate(sources=["https://www.ril.com/investors/disclosures"])), 1)
        self.assertTrue(search._date_matches("28th September 2026", NOW))

    def test_rejects_news_unretrieved_urls_spoofed_domains_and_other_companies(self):
        for url in ("https://news.example.com/story", "https://ril.com.evil.example/filing.pdf",
                    "https://ril.com@evil.example/filing.pdf", "javascript:alert(1)",
                    "https://ril.com:8080/filing.pdf", "https://www.tcs.com/filing.pdf",
                    "https://www.ril.com/investors", "https://www.ril.com/annual-report-2026.pdf",
                    "https://www.nseindia.com/companies-listing/corporate-filings-announcements?symbol=RELIANCE"):
            with self.subTest(url=url):
                self.assertEqual(self.validate({**FILING, "source_url": url}, sources=[url]), [])
        self.assertEqual(self.validate(sources=[]), [])
        self.assertEqual(self.validate({**FILING, "ticker": "TCS"}), [])
        self.assertEqual(self.validate({**FILING, "company_evidence": "Unrelated Limited"}), [])

    def test_rejects_future_old_dates_and_evidence_not_in_document(self):
        for date_text in ("2026-09-29", "2026-08-01", "not-a-date"):
            with self.subTest(date=date_text):
                self.assertEqual(self.validate({**FILING, "published_date": date_text}), [])
        self.assertEqual(self.validate({**FILING, "date_evidence": "2026-09-27"}), [])
        self.assertEqual(self.validate(body="Reliance Industries Limited September 28, 2026 unrelated content"), [])
        self.assertEqual(self.validate(body="Access Denied"), [])

    def test_blocked_document_needs_provider_open_and_is_labeled_less_independent(self):
        with patch.object(search, "_document_text", side_effect=requests.HTTPError("403")):
            self.assertEqual(search.validate_filings(RESULT, COMPANIES, NOW), [])
            rows = search.validate_filings({**RESULT, "opened": [URL]}, COMPANIES, NOW)
        self.assertEqual(rows[0]["search_metadata"]["verification"], "search_provider")

    def test_redirect_to_foreign_domain_is_not_requested(self):
        response = Mock()
        response.is_redirect = True
        response.headers = {"Location": "https://evil.example/filing.pdf"}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch.object(search.requests, "get", return_value=response) as get:
            with self.assertRaises(ValueError):
                search._document_text(URL, "RELIANCE")
        self.assertEqual(get.call_count, 1)

    def test_provider_call_uses_domain_filters_and_rejects_unprovenanced_output(self):
        output = Mock()
        output.model_dump.return_value = {"type": "web_search_call", "status": "completed",
                                          "action": {"type": "search", "sources": [{"url": URL}]}}
        opened = Mock()
        opened.model_dump.return_value = {"type": "web_search_call", "status": "completed",
                                          "action": {"type": "open_page", "url": URL, "sources": None}}
        response = SimpleNamespace(status="completed", output=[output, opened], output_text=json.dumps({"filings": [FILING]}))
        api = Mock()
        api.with_options.return_value.responses.create.return_value = response
        with patch.object(llm, "get_client", return_value=api):
            result = llm.search_filings("search", ["ril.com"], "configured-model")
            kwargs = api.with_options.return_value.responses.create.call_args.kwargs
            self.assertEqual(kwargs["model"], "configured-model")
            self.assertEqual(kwargs["tools"][0]["filters"]["allowed_domains"], ["ril.com"])
            self.assertEqual(kwargs["max_tool_calls"], 8)
            self.assertEqual(result["sources"], [URL])
            response.output = []
            with self.assertRaises(ValueError):
                llm.search_filings("search", ["ril.com"], "configured-model")
            response.status = "incomplete"
            with self.assertRaises(ValueError):
                llm.search_filings("search", ["ril.com"], "configured-model")

    def test_search_is_cached_across_restarts_and_retries_only_after_cooldown(self):
        with patch.object(search, "OPENAI_API_KEY", "test-key"), \
             patch.object(search, "USE_DIGEST_WEB_SEARCH", True), \
             patch.object(search, "search_filings", return_value=RESULT) as discover, \
             patch.object(search, "_document_text", return_value=BODY):
            first = search.fetch_search_announcements(COMPANIES, NOW)
            init_db()
            self.assertEqual(search.fetch_search_announcements(COMPANIES, NOW + timedelta(minutes=5)), first)
            self.assertEqual(discover.call_count, 1)
            search.fetch_search_announcements(COMPANIES, NOW + timedelta(hours=7))
            self.assertEqual(discover.call_count, 2)

    def test_failed_search_preserves_saved_success_and_limits_retries(self):
        with patch.object(search, "OPENAI_API_KEY", "test-key"), \
             patch.object(search, "USE_DIGEST_WEB_SEARCH", True), \
             patch.object(search, "search_filings", return_value=RESULT) as discover, \
             patch.object(search, "_document_text", return_value=BODY):
            search.fetch_search_announcements(COMPANIES, NOW)
            discover.side_effect = RuntimeError("Offline")
            with self.assertLogs(search.logger, level="ERROR"), self.assertRaises(RuntimeError):
                search.fetch_search_announcements(COMPANIES, NOW + timedelta(hours=7))
            with self.assertRaises(RuntimeError):
                search.fetch_search_announcements(COMPANIES, NOW + timedelta(hours=7, minutes=5))
        self.assertEqual(discover.call_count, 2)
        status = search.search_status()
        self.assertEqual(status["succeeded_at"], NOW.isoformat())
        self.assertEqual(status["summary"]["accepted"], 1)
        self.assertIn("unavailable", status["error"])

    def test_missing_key_or_disabled_fallback_never_calls_api(self):
        for key, enabled in (("", True), ("test-key", False)):
            with self.subTest(enabled=enabled), patch.object(search, "OPENAI_API_KEY", key), \
                 patch.object(search, "USE_DIGEST_WEB_SEARCH", enabled), \
                 patch.object(search, "search_filings") as discover:
                with self.assertRaises(RuntimeError):
                    search.fetch_search_announcements(COMPANIES, NOW)
                discover.assert_not_called()

    def test_missing_key_is_handled_in_background_and_all_pages_remain_available(self):
        with get_conn() as conn:
            conn.execute("INSERT INTO feed_state (feed) VALUES ('filings')")
        with patch.object(digest, "USE_LIVE_NSE", True), \
             patch.object(digest, "fetch_recent_announcements", return_value=None), \
             patch.object(digest, "fetch_announcements", return_value=[]), \
             patch.object(search, "OPENAI_API_KEY", ""), \
             patch.object(search, "search_filings") as discover, \
             self.assertLogs("app.feeds", level="ERROR"):
            feeds._job("filings")
        discover.assert_not_called()
        self.assertIsNone(feeds.feed_status("filings")["succeeded_at"])
        client = TestClient(app)
        for url in ("/healthz", "/digest", "/alerts", "/research"):
            self.assertEqual(client.get(url).status_code, 200)
        self.assertIn("OPENAI_API_KEY", client.get("/digest").text)

    def test_exchange_failure_uses_fallback_and_recovery_upgrades_without_duplicate(self):
        item = self.validate()[0]
        with patch.object(digest, "load_portfolio", return_value=PORTFOLIO), \
             patch.object(digest, "USE_LIVE_NSE", True), \
             patch.object(digest, "fetch_recent_announcements", return_value=None) as rss, \
             patch.object(digest, "fetch_announcements", return_value=[]) as api, \
             patch.object(digest, "fetch_search_announcements", return_value=[item]) as discover:
            self.assertEqual(digest.refresh_all(), 1)
            self.assertEqual(digest.refresh_all(), 0)
            rss.return_value = [{**item, "an_dt": "28-Sep-2026 15:30:00", "search_metadata": None}]
            digest.refresh_all()
            discover.reset_mock()
            digest.refresh_all()
            discover.assert_not_called()
            api.reset_mock()
        with get_conn() as conn:
            rows = conn.execute("SELECT * FROM announcements").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "nse_rss")
        self.assertEqual(rows[0]["published_at"], "2026-09-28T10:00:00+00:00")
        self.assertIsNone(rows[0]["raw_json"])
        self.assertFalse(search.search_status()["summary"]["active"])

    def test_date_only_window_overlap_and_mobile_page_explains_coverage(self):
        item = self.validate()[0]
        metadata = json.dumps(item["search_metadata"])
        with get_conn() as conn:
            for id, day in (("recent", "2026-09-27T18:30:00+00:00"), ("old", "2026-09-25T18:30:00+00:00")):
                conn.execute("""INSERT INTO announcements (id,ticker,company,exchange,published_at,headline,
                                category,priority,attachment_url,source,raw_json)
                                VALUES (?,?,?,?,?,?,'M&A','high',?,'web_search',?)""",
                             (id, "RELIANCE", "Reliance Industries", "Company", day, FILING["headline"], URL, metadata))
        search.set_fallback_active(True)
        clock = Mock(wraps=datetime)
        clock.now.return_value = NOW
        with patch.object(digest, "datetime", clock):
            data = digest.get_digest(12)
            response = TestClient(app).get("/digest?hours=12")
        self.assertEqual([r["id"] for r in data["high"]], ["recent"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("Coverage is incomplete", response.text)
        self.assertIn("time unavailable", response.text)
        self.assertIn("Search-discovered", response.text)
        self.assertIn(f'href="{URL}"', response.text)


if __name__ == "__main__":
    unittest.main()
