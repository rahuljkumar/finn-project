import re
import threading
from typing import Literal
from urllib.parse import urlencode
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, HTTPException, Query, Request
from pydantic import BaseModel, Field
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import ALERT_PRICE_MOVE_PCT, ALERT_VOLUME_MULTIPLE, AUTO_REFRESH, load_portfolio
from app.db import get_conn, init_db
from app.jobs import get_job, start_job
from app.feeds import feed_status, poll_feeds, request_refresh
from app.pipeline.alerts import get_all_alerts
from app.pipeline.digest import get_digest
from app.pipeline.enrich import get_management_bio, get_results_card
from app.pipeline.promised_vs_delivered import build_promised_vs_delivered
from app.pipeline.pull_pipeline import CATEGORY_PROMPTS, DEEP_CATEGORIES, document_status, generate_section, get_snapshot, sync_ticker
from app.pipeline.conversation import request_answer
from app.pipeline.news import request_explanation, search_enabled
from app.pipeline.summaries import PRESETS, request_article_summary, summary_groups
from app.pipeline.research_cache import ResearchBusy, cached_research
from app.presentation import display_time, render_markdown, source_url

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
def digest(request: Request, hours: int = Query(default=24, ge=1, le=8760), refresh: bool = False,
           mode: Literal["preset", "custom"] = "preset", ticker: str = Query(default="", max_length=20),
           q: str = Query(default="", max_length=200)):
    if refresh:
        request_refresh("filings", force=True)
    ticker, q = ticker.strip().upper(), q.strip()
    data = get_digest(window_hours=hours, ticker=ticker, query=q)
    groups = summary_groups(data, mode=mode, ticker=ticker, query=q)
    params = urlencode({"hours": hours, "mode": mode, "ticker": ticker, "q": q})
    return templates.TemplateResponse(
        request,
        "digest.html",
        {"title": "FINN · Digest", "active": "digest", "feed": feed_status("filings"),
         "poll_url": f"/digest?{params}", "filter_params": params, "mode": mode,
         "filter_ticker": ticker, "filter_query": q, "companies": all_portfolio_companies(),
         "groups": groups, "automatic": mode == "preset" and hours in PRESETS and not ticker and not q, **data},
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
    names = {c["ticker"]: c.get("name", c["ticker"]) for c in all_portfolio_companies()}
    for alert in data["price_volume"]:
        alert["company"] = names.get(alert["ticker"], alert["ticker"])
        alert["job"] = request_explanation(alert, vol_mult, price_pct)
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
            "news_enabled": search_enabled(vol_mult, price_pct),
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
            "document_feed": feed_status("documents"),
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
    documents = document_status(ticker)
    document_job = get_job(f"documents:{ticker}")
    if not documents["ready"]:
        document_job = start_job(f"documents:{ticker}", "documents", lambda: sync_ticker(ticker),
                                 max_age=86400, requires_model=False)
    return templates.TemplateResponse(
        request,
        "company.html",
        {
            "title": f"FINN · {ticker}",
            "active": "research",
            "ticker": ticker,
            "company_name": names.get(ticker),
            "categories": PULL_CATEGORY_LABELS,
            "documents": documents, "document_job": document_job,
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
            fundamentals = get_snapshot(ticker)
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


def _valid_ticker(ticker: str) -> str:
    ticker = ticker.strip().upper()
    if not TICKER_RE.fullmatch(ticker):
        raise HTTPException(422, "Invalid ticker")
    return ticker


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=6000)


class ChatQuestion(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=12)


@app.post("/research/{ticker}/chat")
def research_chat(ticker: str, body: ChatQuestion):
    ticker = _valid_ticker(ticker)
    question = body.question.strip()
    if not question:
        raise HTTPException(422, "Enter a question")
    job = request_answer(ticker, question, [turn.model_dump() for turn in body.history])
    return {"job_key": job["job_key"], "state": job["state"], "error": job.get("error")}


@app.get("/ai/jobs/{key}")
def ai_job_json(key: str):
    job = get_job(key)
    if not job:
        raise HTTPException(404, "Answer not found")
    result = job.get("result")
    return {"state": job["state"], "error": job.get("error"), "result": result,
            "html": templates.env.get_template("partials/ai_content.html").render(result=result) if result else None}


@app.get("/ai/results/{key}")
def ai_job_partial(request: Request, key: str):
    job = get_job(key)
    if not job:
        raise HTTPException(404, "Summary not found")
    return templates.TemplateResponse(request, "partials/ai_job.html", {"job": job})


@app.get("/research/{ticker}/documents")
def research_document_status(request: Request, ticker: str):
    ticker = _valid_ticker(ticker)
    return templates.TemplateResponse(request, "partials/research_documents.html",
        {"ticker": ticker, "documents": document_status(ticker), "document_job": get_job(f"documents:{ticker}")})


@app.post("/research/{ticker}/documents")
def research_document_sync(request: Request, ticker: str):
    ticker = _valid_ticker(ticker)
    job = start_job(f"documents:{ticker}", "documents", lambda: sync_ticker(ticker),
                    max_age=0, retry=True, requires_model=False)
    return templates.TemplateResponse(request, "partials/research_documents.html",
        {"ticker": ticker, "documents": document_status(ticker), "document_job": job})


@app.post("/research/sync-documents")
def research_portfolio_sync(request: Request):
    request_refresh("documents", force=True)
    return templates.TemplateResponse(request, "partials/document_feed.html", {"document_feed": feed_status("documents")})


@app.get("/feeds/documents")
def research_document_feed(request: Request):
    return templates.TemplateResponse(request, "partials/document_feed.html", {"document_feed": feed_status("documents")})


@app.post("/digest/summaries")
def digest_summaries(request: Request, hours: int = Form(default=24, ge=1, le=8760),
                     mode: Literal["preset", "custom"] = Form(default="custom"),
                     ticker: str = Form(default="", max_length=20), q: str = Form(default="", max_length=200)):
    ticker, q = ticker.strip().upper(), q.strip()
    data = get_digest(hours, ticker=ticker, query=q)
    groups = summary_groups(data, mode=mode, ticker=ticker, query=q, generate=True, retry=True)
    return templates.TemplateResponse(request, "partials/digest_groups.html", {"groups": groups})


@app.post("/digest/articles/{ann_id}/summary")
def digest_article_summary(request: Request, ann_id: str):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM announcements WHERE id=?", (ann_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Announcement not found")
    return templates.TemplateResponse(request, "partials/ai_job.html", {"job": request_article_summary(dict(row))})


@app.post("/alerts/{ticker}/explanation")
def retry_alert_explanation(request: Request, ticker: str,
                            vol_mult: float = Query(default=ALERT_VOLUME_MULTIPLE, ge=1),
                            price_pct: float = Query(default=ALERT_PRICE_MOVE_PCT, ge=0)):
    ticker = _valid_ticker(ticker)
    data = get_all_alerts(volume_multiple=vol_mult, price_move_pct=price_pct)
    alert = next((a for a in data["price_volume"] if a["ticker"] == ticker), None)
    job = request_explanation(alert, vol_mult, price_pct, retry=True) if alert else None
    if not job:
        return templates.TemplateResponse(request, "partials/enrich_error.html", {"message": "News search is disabled for this selection."})
    return templates.TemplateResponse(request, "partials/ai_job.html", {"job": job})
