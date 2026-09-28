import re
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ALERT_PRICE_MOVE_PCT, ALERT_VOLUME_MULTIPLE, AUTO_REFRESH, load_portfolio
from app.db import get_conn, init_db
from app.feeds import feed_status, poll_feeds, request_refresh
from app.pipeline.alerts import get_all_alerts
from app.pipeline.digest import get_digest
from app.pipeline.enrich import get_management_bio, get_results_card
from app.pipeline.promised_vs_delivered import build_promised_vs_delivered
from app.pipeline.pull_pipeline import CATEGORY_PROMPTS, DEEP_CATEGORIES, generate_section
from app.pipeline.research_cache import ResearchBusy, cached_research
from app.presentation import display_time, render_markdown, source_url
from app.sources.screener_client import get_fundamentals

TICKER_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")

PULL_CATEGORY_LABELS = [
    ("business_snapshot", "Business Snapshot"),
    ("financial_snapshot", "Financial Snapshot"),
    ("trajectory", "Trajectory"),
    ("balance_sheet", "Balance Sheet"),
    ("cash_quality", "Cash Quality"),
    ("narrative_vs_numbers", "Narrative vs. Numbers"),
    ("guidance", "Guidance"),
    ("bull_bear", "Bull vs. Bear"),
]

templates = Jinja2Templates(directory="app/templates")
templates.env.filters["markdown"] = render_markdown
templates.env.filters["source_url"] = source_url
templates.env.filters["display_time"] = display_time


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    stop = threading.Event()
    if AUTO_REFRESH:
        threading.Thread(target=poll_feeds, args=(stop,), daemon=True, name="finn-poller").start()
    try:
        yield
    finally:
        stop.set()


app = FastAPI(title="FINN", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/")
async def home(request: Request):
    return templates.TemplateResponse(
        request,
        "home.html",
        {"title": "FINN"},
    )


@app.get("/digest")
# Blocking data/model calls run in FastAPI's worker pool so health checks stay responsive.
def digest(request: Request, hours: int = Query(default=24, ge=1, le=8760), refresh: bool = False):
    if refresh:
        request_refresh("filings", force=True)
    data = get_digest(window_hours=hours)
    return templates.TemplateResponse(
        request,
        "digest.html",
        {"title": "FINN · Digest", "active": "digest", "feed": feed_status("filings"),
         "poll_url": f"/digest?hours={hours}", **data},
    )


@app.get("/alerts")
def alerts(
    request: Request,
    refresh: bool = False,
    vol_mult: float = Query(default=ALERT_VOLUME_MULTIPLE, ge=1),
    price_pct: float = Query(default=ALERT_PRICE_MOVE_PCT, ge=0),
):
    if refresh:
        request_refresh("prices", force=True)
    feed = feed_status("prices")
    refresh_summary = feed["summary"]
    data = get_all_alerts(volume_multiple=vol_mult, price_move_pct=price_pct)
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "title": "FINN · Alerts",
            "active": "alerts",
            "vol_mult": vol_mult,
            "price_pct": price_pct,
            "refresh_summary": refresh_summary,
            "feed": feed,
            "poll_url": f"/alerts?vol_mult={vol_mult}&price_pct={price_pct}",
            **data,
        },
    )


@app.get("/enrich/{ann_id}")
def enrich(request: Request, ann_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM announcements WHERE id = ?", (ann_id,)).fetchone()
    if row is None:
        return templates.TemplateResponse(
            request, "partials/enrich_error.html", {"message": "Announcement not found."}
        )
    item = dict(row)

    if item["category"] == "management_change":
        try:
            data = get_management_bio(item["headline"], item["company"])
        except Exception:
            return templates.TemplateResponse(
                request, "partials/enrich_error.html", {"message": "Couldn't look this up right now."}
            )
        return templates.TemplateResponse(request, "partials/enrich_bio.html", {"data": data})

    if item["category"] == "results":
        try:
            data = get_results_card(item["ticker"])
        except Exception:
            data = None
        if not data:
            return templates.TemplateResponse(
                request, "partials/enrich_error.html", {"message": "No quarterly data available yet."}
            )
        return templates.TemplateResponse(request, "partials/enrich_results.html", {"data": data})

    return templates.TemplateResponse(request, "partials/enrich_error.html", {"message": ""})


@app.get("/research")
async def research(request: Request):
    data = load_portfolio()
    return templates.TemplateResponse(
        request,
        "research.html",
        {
            "title": "FINN · Research",
            "active": "research",
            "portfolio": data["portfolio"],
            "adhoc": data["adhoc"],
        },
    )


@app.get("/research/lookup")
async def research_lookup(ticker: str):
    return RedirectResponse(url=f"/research/{ticker.strip().upper()}")


@app.get("/research/{ticker}")
async def company(request: Request, ticker: str):
    ticker = ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        return templates.TemplateResponse(
            request,
            "research.html",
            {
                "title": "FINN · Research",
                "active": "research",
                "portfolio": load_portfolio()["portfolio"],
                "adhoc": load_portfolio()["adhoc"],
                "error": f"'{ticker}' doesn't look like a valid NSE ticker.",
            },
        )
    names = {c["ticker"]: c["name"] for c in all_portfolio_companies()}
    return templates.TemplateResponse(
        request,
        "company.html",
        {
            "title": f"FINN · {ticker}",
            "active": "research",
            "ticker": ticker,
            "company_name": names.get(ticker),
            "categories": PULL_CATEGORY_LABELS,
        },
    )


@app.get("/research/{ticker}/section/promised_vs_delivered")
def company_pvd(request: Request, ticker: str):
    ticker = ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        return templates.TemplateResponse(
            request, "partials/enrich_error.html", {"message": "Invalid ticker."}
        )
    try:
        result = cached_research(ticker, "promised_vs_delivered", lambda: build_promised_vs_delivered(ticker))
    except ResearchBusy as exc:
        return templates.TemplateResponse(request, "partials/enrich_error.html", {"message": str(exc)})
    except Exception:
        return templates.TemplateResponse(
            request, "partials/enrich_error.html", {"message": "Couldn't generate this right now."}
        )
    return templates.TemplateResponse(
        request, "partials/promised_vs_delivered.html", {"quarters": result["data"], **result}
    )


@app.get("/research/{ticker}/section/{category}")
def company_section(request: Request, ticker: str, category: str):
    if category not in CATEGORY_PROMPTS:
        return templates.TemplateResponse(
            request, "partials/enrich_error.html", {"message": "Unknown section."}
        )
    ticker = ticker.strip().upper()
    if not TICKER_RE.match(ticker):
        return templates.TemplateResponse(
            request, "partials/enrich_error.html", {"message": "Invalid ticker."}
        )
    def generate():
        structured = None
        if category in DEEP_CATEGORIES or category in ("balance_sheet", "cash_quality"):
            fundamentals = get_fundamentals(ticker)
            if fundamentals:
                structured = {
                    "financial_snapshot": fundamentals["profit_loss"],
                    "trajectory": fundamentals["profit_loss"],
                    # working-capital cycle (debtor/inventory/payable days,
                    # ROCE) lives in the ratios table, not balance_sheet --
                    # the brief's "balance sheet" category explicitly wants both
                    "balance_sheet": {
                        "balance_sheet": fundamentals["balance_sheet"],
                        "ratios": fundamentals["ratios"],
                    },
                    "cash_quality": fundamentals["cash_flow"],
                }.get(category)
        return generate_section(ticker, category, structured=structured)
    try:
        result = cached_research(ticker, category, generate)
    except ResearchBusy as exc:
        return templates.TemplateResponse(request, "partials/enrich_error.html", {"message": str(exc)})
    except Exception:
        return templates.TemplateResponse(
            request, "partials/enrich_error.html", {"message": "Couldn't generate this right now."}
        )
    return templates.TemplateResponse(request, "partials/pull_section.html", result)


def all_portfolio_companies() -> list[dict]:
    data = load_portfolio()
    return data["portfolio"] + data["adhoc"]
