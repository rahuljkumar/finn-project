import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
CHEAP_MODEL = os.getenv("CHEAP_MODEL", "gpt-4o-mini")
REASONING_MODEL = os.getenv("REASONING_MODEL", "gpt-4o")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")

DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "finn.db"))

USE_LIVE_NSE = os.getenv("USE_LIVE_NSE", "true").lower() == "true"
USE_LIVE_BSE = os.getenv("USE_LIVE_BSE", "true").lower() == "true"
USE_LIVE_SCREENER = os.getenv("USE_LIVE_SCREENER", "true").lower() == "true"

ALERT_VOLUME_MULTIPLE = float(os.getenv("ALERT_VOLUME_MULTIPLE", "2.0"))
ALERT_PRICE_MOVE_PCT = float(os.getenv("ALERT_PRICE_MOVE_PCT", "5.0"))

SEED_DATA_DIR = BASE_DIR / "seed_data"
PORTFOLIO_FILE = SEED_DATA_DIR / "portfolio.json"

# Rule-based priority mapping for the 17 NSE/BSE announcement categories
# (see AI_Engineer_Build_Challenge_Reference.pdf, Section 1)
CATEGORY_PRIORITY = {
    "results": "high",
    "M&A": "high",
    "management_change": "high",
    "fund_raise": "high",
    "litigation": "high",
    "credit_rating": "medium",  # mostly routine reaffirmations in practice;
    # NSE's own text field doesn't say upgrade/downgrade/reaffirm without
    # opening the PDF, so this can't be split further without an LLM call
    # per item -- demoting the whole category keeps genuine results/M&A/
    # management-change events from getting buried under rating noise.
    "dividend": "medium",
    "bonus_split": "medium",
    "capex": "medium",
    "earnings_call": "low",
    "investor_meetings": "low",
    "press_release": "low",
    "postal_ballot": "low",
    "agm_egm": "low",
    "pledging": "low",
    "insider_trading": "low",
    "unclassified": "low",
}


def load_portfolio() -> dict:
    with open(PORTFOLIO_FILE, encoding="utf-8") as f:
        return json.load(f)


def all_tickers() -> list[str]:
    data = load_portfolio()
    return [c["ticker"] for c in data["portfolio"]] + [c["ticker"] for c in data["adhoc"]]
