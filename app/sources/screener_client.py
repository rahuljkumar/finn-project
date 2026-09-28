"""screener.in company-page scraping for fundamentals + primary-source
document links.

Only individual company pages are fetched (/company/<TICKER>/...), which
screener.in's robots.txt permits -- search/listing/screen pages are not
touched. This is also the path to Pull mode "generalizing to any NSE
ticker": screener.in covers nearly every listed company.
"""

import logging
import re

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
}

ROW_LABELS = {
    "Sales": "revenue",
    "Sales+": "revenue",
    "Operating Profit": "ebitda",
    "Profit before tax": "pbt",
    "Net Profit": "pat",
    "Net Profit+": "pat",
}


def _num(text: str) -> float | None:
    text = text.strip().replace(",", "").replace("%", "")
    if not text or text in ("-",):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def fetch_soup(ticker: str) -> BeautifulSoup | None:
    for variant in ("consolidated", ""):
        url = f"https://www.screener.in/company/{ticker}/{variant + '/' if variant else ''}"
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            if resp.status_code == 200 and "company" in resp.url:
                return BeautifulSoup(resp.text, "lxml")
        except requests.RequestException as e:
            logger.warning("screener.in fetch failed for %s (%s): %s", ticker, variant, e)
    return None


def get_quarterly_table(soup: BeautifulSoup) -> dict:
    """Returns {quarter_label: {revenue, ebitda, pbt, pat}} in column order (oldest->newest)."""
    section = soup.find("section", id="quarters")
    if not section:
        return {}
    table = section.find("table")
    headers = [th.get_text(strip=True) for th in table.find("thead").find_all("th")][1:]
    quarters = {h: {} for h in headers}

    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        label = cells[0].get_text(strip=True).rstrip("+")
        key = ROW_LABELS.get(cells[0].get_text(strip=True))
        if not key:
            continue
        values = [_num(c.get_text()) for c in cells[1:]]
        for h, v in zip(headers, values):
            quarters[h][key] = v

    return quarters


def get_annual_table(soup: BeautifulSoup, section_id: str) -> dict:
    """Generic parser for screener's annual statement tables (profit-loss,
    balance-sheet, cash-flow, ratios) -- all share the same
    label-row / period-column structure. Returns {row_label: {period: value}}."""
    section = soup.find("section", id=section_id)
    if not section:
        return {}
    table = section.find("table")
    if not table:
        return {}
    periods = [th.get_text(strip=True) for th in table.find("thead").find_all("th")][1:]

    result = {}
    for row in table.find("tbody").find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        label = cells[0].get_text(strip=True).rstrip("+")
        if not label or label.lower() == "raw pdf":
            continue
        values = [c.get_text(strip=True) for c in cells[1:]]
        result[label] = dict(zip(periods, values))
    return result


def get_fundamentals(ticker: str) -> dict | None:
    """All structured statement data for a ticker in one page fetch --
    the numeric backbone for the Pull-mode categories that don't need
    prose retrieval (financial snapshot, trajectory, balance sheet,
    cash quality)."""
    soup = fetch_soup(ticker)
    if soup is None:
        return None
    return {
        "profit_loss": get_annual_table(soup, "profit-loss"),
        "balance_sheet": get_annual_table(soup, "balance-sheet"),
        "cash_flow": get_annual_table(soup, "cash-flow"),
        "ratios": get_annual_table(soup, "ratios"),
        "documents": get_documents(soup),
    }


def get_qoq_yoy(ticker: str) -> dict | None:
    soup = fetch_soup(ticker)
    if soup is None:
        return None
    quarters = get_quarterly_table(soup)
    labels = list(quarters.keys())
    if len(labels) < 5:
        return None

    latest_label = labels[-1]
    qoq_label = labels[-2]
    yoy_label = labels[-5]  # 4 quarters back

    def pct_change(new: float | None, old: float | None) -> float | None:
        if new is None or old is None or old == 0:
            return None
        return (new - old) / abs(old) * 100

    latest = quarters[latest_label]
    qoq = quarters[qoq_label]
    yoy = quarters[yoy_label]

    result = {"quarter": latest_label, "qoq_quarter": qoq_label, "yoy_quarter": yoy_label, "metrics": {}}
    for metric in ("revenue", "ebitda", "pbt", "pat"):
        result["metrics"][metric] = {
            "value": latest.get(metric),
            "qoq_pct": pct_change(latest.get(metric), qoq.get(metric)),
            "yoy_pct": pct_change(latest.get(metric), yoy.get(metric)),
        }
    return result


def get_documents(soup: BeautifulSoup) -> dict:
    section = soup.find("section", id="documents")
    if not section:
        return {"annual_reports": [], "concalls": []}

    annual_reports = []
    ar_heading = next((h for h in section.find_all("h3") if "annual report" in h.get_text().lower()), None)
    if ar_heading:
        container = ar_heading.find_parent("div").find_next("ul")
        if container:
            for a in container.find_all("a"):
                text = a.get_text(strip=True)
                m = re.search(r"(20\d{2})", text)
                if m and a.get("href"):
                    annual_reports.append({"year": int(m.group(1)), "url": a["href"]})

    concalls = []
    cc_heading = next((h for h in section.find_all("h3") if "concall" in h.get_text().lower()), None)
    if cc_heading:
        container = cc_heading.find_parent("div").find_next("ul")
        if container:
            for li in container.find_all("li", recursive=False):
                date_div = li.find("div")
                label = date_div.get_text(strip=True) if date_div else None
                transcript = None
                ppt = None
                for a in li.find_all("a"):
                    title = (a.get("title") or a.get_text(strip=True)).lower()
                    if "transcript" in title:
                        transcript = a.get("href")
                    elif "ppt" in title or "presentation" in title:
                        ppt = a.get("href")
                if label:
                    concalls.append({"label": label, "transcript_url": transcript, "ppt_url": ppt})

    return {"annual_reports": annual_reports, "concalls": concalls}
