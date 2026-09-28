"""Official NSE announcement RSS, published on NSE's RSS page.

This feed contains recent filings, not historical coverage. Regular polling
and persistent storage build history; the JSON endpoint can backfill it.
"""

import hashlib
import re
from urllib.parse import urlparse

import requests
from lxml import etree

RSS_URL = "https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml"


def _name(value: str) -> str:
    value = re.sub(r"\b(limited|ltd)\b", "", value.lower())
    return re.sub(r"[^a-z0-9]", "", value)


def parse_announcements(content: bytes, companies: list[dict]) -> list[dict]:
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    root = etree.fromstring(content, parser)
    if root.tag != "rss" or root.find("channel") is None:
        raise ValueError("Invalid NSE RSS response")
    names = {_name(c["name"].split("(", 1)[0]): c["ticker"] for c in companies}
    for ticker, official_name in {"SUNPHARMA": "Sun Pharmaceutical Industries", "IRCTC": "Indian Railway Catering And Tourism Corporation"}.items():
        if any(c["ticker"] == ticker for c in companies):
            names[_name(official_name)] = ticker
    # Some portfolio names are shortened. Match the exact ticker prefix in
    # official PDF filenames as well; never use fuzzy company-name matching.
    tickers = {c["ticker"] for c in companies}
    matched = []
    for item in root.findall("./channel/item"):
        url = (item.findtext("link") or "").strip()
        if urlparse(url).scheme not in {"http", "https"}:
            continue
        ticker = names.get(_name(item.findtext("title") or ""))
        prefix = urlparse(url).path.rsplit("/", 1)[-1].split("_", 1)[0].upper()
        ticker = ticker or (prefix if prefix in tickers else None)
        if not ticker:
            continue
        text, _, subject = (item.findtext("description") or "").partition("|SUBJECT:")
        date = (item.findtext("pubDate") or "").strip()
        matched.append({
            "symbol": ticker, "desc": subject.strip(), "attchmntText": text.strip(),
            "attchmntFile": url, "an_dt": date,
            "seq_id": hashlib.sha256(f"{ticker}|{url}|{date}".encode()).hexdigest(),
        })
    return matched


def fetch_recent_announcements(companies: list[dict]) -> list[dict]:
    response = requests.get(RSS_URL, timeout=(5, 10), headers={"User-Agent": "FINN/1.0 RSS reader"})
    response.raise_for_status()
    return parse_announcements(response.content, companies)
