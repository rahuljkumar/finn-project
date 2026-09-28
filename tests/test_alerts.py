import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from app.db import get_conn, init_db
from app.main import app
from app.pipeline.alerts import get_price_data_status
from app.sources.yfinance_client import detect_anomaly, persist_history


PORTFOLIO = {"portfolio": [{"ticker": "AAA"}, {"ticker": "BBB"}], "adhoc": []}


class AlertsTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        db_patch = patch("app.db.DB_PATH", str(Path(folder.name) / "alerts.db"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        portfolio_patch = patch("app.pipeline.alerts.load_portfolio", return_value=PORTFOLIO)
        portfolio_patch.start()
        self.addCleanup(portfolio_patch.stop)
        init_db()
        self.client = TestClient(app)

    def add_price(self, ticker, volume=100, average=100, change=0):
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO prices VALUES (?, ?, ?, ?, ?, ?)",
                (ticker, "2026-09-28", 100, volume, average, change),
            )

    def test_empty_database_explains_missing_data(self):
        response = self.client.get("/alerts?vol_mult=1&price_pct=0.5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("No price data loaded yet", response.text)
        self.assertNotIn("No matching alerts", response.text)
        self.assertNotIn("No active alerts", response.text)
        self.assertEqual(get_price_data_status()["available"], 0)

    def test_low_thresholds_match_volume_and_positive_or_negative_price_moves(self):
        self.add_price("AAA", volume=110, change=0.6)
        self.add_price("BBB", volume=90, change=-0.5)
        response = self.client.get("/alerts?vol_mult=1&price_pct=0.5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Price &amp; volume (2)", response.text)
        self.assertIn("Price moved +0.6%", response.text)
        self.assertIn("Price moved -0.5%", response.text)
        self.assertIn("1.1x its 20-day average", response.text)
        self.assertIn("Price data: 2 of 2 stocks", response.text)

    def test_real_no_match_is_distinct_from_missing_data(self):
        self.add_price("AAA", change=0.1)
        response = self.client.get("/alerts?vol_mult=2&price_pct=5")
        self.assertIn("No matching alerts at these thresholds", response.text)
        self.assertIn("Price data: 1 of 2 stocks", response.text)
        self.assertNotIn("No price data loaded", response.text)

    def test_price_move_is_checked_without_a_volume_average(self):
        self.add_price("AAA", volume=None, average=None, change=-0.5)
        alert = detect_anomaly("AAA", volume_multiple=1, price_move_pct=0.5)
        self.assertEqual(alert["reasons"], ["Price moved -0.5% in a day"])
        self.assertEqual(get_price_data_status()["available"], 1)

    def test_volume_alert_renders_when_price_change_is_unavailable(self):
        self.add_price("AAA", volume=210, average=100, change=None)
        response = self.client.get("/alerts?vol_mult=2&price_pct=5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Price &amp; volume (1)", response.text)

    def test_refresh_preserves_thresholds_and_continues_after_a_failed_stock(self):
        self.add_price("BBB", volume=90, change=-0.7)

        def refresh(ticker):
            if ticker == "BBB":
                raise RuntimeError("Provider unavailable")
            history = pd.DataFrame([{
                "Date": pd.Timestamp("2026-09-28"), "Close": 100,
                "Volume": 110, "avg_volume_20d": 100, "pct_change": 0.6,
            }])
            persist_history(ticker, history)
            return history

        with patch("app.pipeline.alerts.refresh_ticker", side_effect=refresh) as fetch, \
             self.assertLogs("app.pipeline.alerts", level="WARNING"):
            response = self.client.get("/alerts?refresh=true&vol_mult=1&price_pct=0.5")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("Updated prices for 1 of 2 stocks", response.text)
        self.assertIn("Could not fetch prices for 1 of your stocks", response.text)
        self.assertIn("Price &amp; volume (2)", response.text)
        soup = BeautifulSoup(response.text, "html.parser")
        self.assertEqual(soup.find("input", {"name": "vol_mult"})["value"], "1.0")
        self.assertEqual(soup.find("input", {"name": "price_pct"})["value"], "0.5")
        link = soup.find("a", string="Refresh")["href"]
        self.assertIn("vol_mult=1.0", link)
        self.assertIn("price_pct=0.5", link)
        self.assertEqual(get_price_data_status()["loaded"], 2)

    def test_empty_provider_response_is_reported(self):
        with patch("app.pipeline.alerts.refresh_ticker", return_value=pd.DataFrame()):
            response = self.client.get("/alerts?refresh=true&vol_mult=1&price_pct=0.5")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Updated prices for 0 of 2 stocks", response.text)
        self.assertIn("No price data loaded yet", response.text)

    def test_incomplete_history_is_not_reported_as_no_alerts(self):
        self.add_price("AAA", average=None, change=None)
        response = self.client.get("/alerts")
        self.assertIn("Not enough price history", response.text)
        self.assertNotIn("No matching alerts", response.text)

    def test_invalid_negative_thresholds_are_rejected(self):
        for query in ("vol_mult=0.5&price_pct=0.5", "vol_mult=1&price_pct=-0.5"):
            self.assertEqual(self.client.get("/alerts?" + query).status_code, 422)


if __name__ == "__main__":
    unittest.main()
