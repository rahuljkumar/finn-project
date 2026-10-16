# FINN — Financial Investment Agent

A mobile-first portfolio intelligence agent with two modes:

- **Push** — a windowed digest of portfolio filings/announcements, filtered by priority, plus triggered alerts on unusual price/volume moves with sourced explanations.
- **Pull** — on-demand company research grounded only in primary sources (annual reports, concall transcripts): business snapshot, financial snapshot, trajectory, balance sheet, cash quality, narrative vs. numbers, guidance, and bull vs. bear — including a "promised vs. delivered" view across the last four quarters.

## Stack

Single FastAPI service (Python) rendering server-side Jinja2 templates with HTMX for partial updates, Tailwind (CDN) for styling, and Alpine.js for light client-side interactivity. Data: official NSE daily reports for EOD price/volume, Yahoo Finance as a secondary price source, official NSE RSS for recent announcements, the NSE JSON endpoint for historical backfill, and `screener.in` company pages for fundamentals and document links. No market-data API key is required for these feeds. Direct BSE ingestion is not implemented; Digest's web-search fallback can discover original BSE disclosures.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/activate       # or `source .venv/bin/activate` on macOS/Linux
python -m pip install -r requirements.txt
cp .env.example .env         # PowerShell: Copy-Item .env.example .env
python -m uvicorn app.main:app --reload
```

If Windows Application Control blocks `uvicorn.exe`, run Uvicorn through the virtual environment's Python interpreter:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000`.

## Assignment demo: Research, Alerts, and Digest

These upgrades use the existing **direct OpenAI API key**, Screener company pages, and local storage. No Azure setup or Screener API access is needed.

- **Research:** report discovery runs daily at **18:30 IST** while the app is running, with a catch-up check after startup. New PDFs are chunked with page references and embedded; unchanged reports reuse their vectors. The **20 portfolio stocks × nine topics = 180 Research sections** are prepared in the background and stored persistently. Current answers are embedded in each company page so prepared topics expand immediately. A company without an index starts report preparation in the background.
- **Alerts:** news searches run only when **both selected filters are at least 2× volume and 5% price**. Either actual volume or absolute price movement must exceed its selected threshold. Explanations distinguish a reported catalyst, possibly related news, and no reliable explanation. Source links and cached results appear below the move.
- **Digest:** priority and notable filings share **one summary per ticker**. Standard **24h, 7d, and 30d** views prepare summaries automatically. Custom hours, company filters, and text filters require **Generate AI summary** and summarize only the selected disclosures. Routine filings have individual manual buttons. Management summaries extract names and roles from the document, using sourced public biographies when background is missing.

Prepare a company before recording, then repeat the same command to inspect the unchanged count:

```powershell
.\.venv\Scripts\python.exe -m app.pipeline.sync_documents TCS
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

Omit `TCS` to check the whole portfolio, or pass multiple ticker symbols. The Research screen also provides manual report-check buttons. Failed downloads preserve existing reports; summaries display incomplete source coverage and offer retry actions.

### One-time two-year Research backfill before recording

Stop the local app during the backfill so its background workers do not write the same indexes. From the project root, run:

```powershell
.\.venv\Scripts\python.exe -m app.pipeline.backfill_research
```

This loads the **20 companies under Your portfolio** in `seed_data/portfolio.json` and prepares **only the last two years of Research reports**. It downloads annual reports for the two most recent reporting years and concall transcripts whose dated labels fall within the two-year window, then chunks/embeds new reports into the same index used by Research. It also refreshes stored financial tables. Older links are filtered before any download or embedding call. Existing PDFs and vectors are reused, and a missing PDF is restored even when its vectors are already present.

The cutoff is calculated using today's IST date. On **14 October 2026**, annual report years are **2025 and 2026**, and the concall cutoff is **14 October 2024**. Screener usually provides month-only concall labels; these use the first day of the month, so an **October 2024** call without an exact day is excluded conservatively. Links with unrecognizable dates are skipped and reported for review. The JSON report records the date window used.

Progress prints per report and is saved to `data/research_backfill_report.json` (or the directory configured by `DB_PATH`). Failures include their report labels and URLs; the script continues with other reports and companies. Exit code **0** means complete, **1** means partial coverage, and **130** means interrupted. Rerun the command to resume; completed reports are not embedded again. Source sites may block individual downloads, and only reports with extractable text can be indexed.

Useful options:

```powershell
# Inspect the selection without downloads or OpenAI calls.
.\.venv\Scripts\python.exe -m app.pipeline.backfill_research --dry-run
# Also prepare the five ad-hoc watchlist companies.
.\.venv\Scripts\python.exe -m app.pipeline.backfill_research --include-adhoc
# Retry only selected companies.
.\.venv\Scripts\python.exe -m app.pipeline.backfill_research TCS INFY
```

The script uses your existing `.env` and OpenAI embedding model. Once it finishes, start the app normally and open Research. Daily sync retains the backfilled history and continues checking recent report links.

Keep your existing model settings. Optional `WEB_SEARCH_MODEL` defaults to `DIGEST_SEARCH_MODEL`, then `REASONING_MODEL`; it must support Responses web search and structured output. `DOCUMENT_SYNC_HOUR` and `DOCUMENT_SYNC_MINUTE` change the report-check time in IST. `AUTO_REFRESH=false` disables scheduled feed and report checks, while manual actions still work. AI work uses background jobs so the tabs remain usable during generation.

Suggested recording: ask a Research question and follow-up, open a citation, compare enabled and suppressed Alert searches, then show an automatic Digest preset, a filtered manual summary, and an individual routine summary. See [the upgrade plan](docs/UPGRADE_PLAN.md) for the agreed product rules.

## Deployment

Deploy as a Docker web service on Render's free tier. Push the project to a private GitHub repository, then create a Render Blueprint using the included `render.yaml`. Set `OPENAI_API_KEY`, `CHEAP_MODEL`, and `REASONING_MODEL` in Render to the values used by your local environment. The Blueprint selects the Free plan and `/healthz` health check; the Dockerfile binds to Render's `$PORT`.

For a manual Web Service setup, select Docker, Free, `./Dockerfile`, and health check path `/healthz`. Add the same environment variables plus `EMBEDDING_MODEL=text-embedding-3-small` and `DB_PATH=/srv/data/finn.db`.

The Free plan is useful for trying the app, but does not provide continuous operation or durable local storage. Free instances sleep after 15 minutes of inactivity and discard SQLite data and caches on sleep, restart, or redeploy. A paid instance also needs a persistent disk to preserve its files. Do not commit `.env` or the challenge PDFs. `.dockerignore` excludes local secrets and cached data from the image.

### Continuous demo on Render

For this single-instance app, start with paid compute providing **1 CPU and 2 GB RAM**, and attach a **5 GB persistent disk** for the two-year research corpus and caches. Monitor memory and disk usage as the corpus grows. A paid workspace subscription or separate database service is not required. Compute, workspace subscriptions, and disks are billed separately; confirm current prices on [Render's pricing page](https://render.com/pricing).

1. In the existing service's **Compute** page, select the **1 CPU / 2 GB RAM** compute plan.
2. Open **Disk**, add a disk of **5 GB**, and set its mount path to **`/srv/data`**. Adding a disk triggers a deploy. Only files under that mount path persist; see [Render's disk documentation](https://render.com/docs/disks).
3. Keep **`DB_PATH=/srv/data/finn.db`**, **`USE_LIVE_NSE=true`**, and **`AUTO_REFRESH=true`** in the service's environment. Keep the existing OpenAI key and model settings. All database, PDF, vector-index and report caches now use the database's parent directory.
4. Deploy the latest GitHub commit. The first deploy with a new disk starts with an empty database; local laptop data is not copied automatically.

The included `render.yaml` still selects Free for new Blueprint deployments. For an existing manually created service, configure paid compute and the disk in the dashboard. Keep one service instance and one Uvicorn worker: the refresh coordinator and research memory limit are process-local. A disk-backed service has a brief interruption during redeployment.

### Freshness and outage behavior

- On startup, background workers begin loading filings and prices and check whether the latest document-sync slot is due. Filings are polled every **5 minutes**, prices every **30 minutes**, and research documents daily at **18:30 IST** while the service is running. `AUTO_REFRESH=false` disables scheduled polling; manual Refresh still works.
- Refresh returns the page immediately and retains saved results while fetching. The page polls until the job finishes. Repeated clicks reuse the running job and have a 60-second cooldown.
- Digest saves recent disclosures as soon as its source fetch finishes, applying known categories without model calls. New disclosures that need AI classification appear under **New filings** immediately; their pending inputs survive restarts and provider failures. Classification and historical backfill run independently, so neither holds up a new recent-feed refresh. Digest polls only its results area every two seconds during this work, preserving filter inputs, routine expansion, and open supporting filings. Feed jobs and classification batches log elapsed times for diagnosing cloud delays.
- Automatic Digest summaries have one dedicated background worker; chat and explicit AI actions retain two separate workers. Duplicate jobs and completed results are still shared across visitors. First-time model answers and upstream source outages can still take time, but cached cards remain available while that work runs.
- Prices are **end-of-day**, not streaming quotes. The official CSV source loads about 45 calendar days initially, then catches up from saved history. Its reported previous close determines each daily price change; a full 20-session volume average is required for volume alerts. Missing/blocked reports fall back to Yahoo. Both sources can still become unavailable.
- RSS contains recent announcements, usually a short window. The app retains them over time and attempts a 120-day JSON backfill once a day until it succeeds. The UI labels incomplete historical coverage. A fresh deployment cannot promise a complete 7-day or 30-day digest when historical access is blocked.
- When both direct filing sources fail, Digest uses OpenAI web search with the **existing `OPENAI_API_KEY`**. It searches the last 30 days on NSE, BSE and reviewed portfolio-company domains, prioritizes the latest 48 hours and 7 days, accepts at most 30 original disclosures with company/date evidence, and saves them in SQLite. Evidence is checked against the actual document, including original PDF links found through investor indexes. If direct access fails, only an explicitly provider-opened document is accepted and labeled accordingly. News stories are excluded by the search instructions; generic investor/quote pages and annual reports are also rejected by URL checks. Search discovery is **incomplete**, and does not satisfy a guarantee of every filing.
- Fallback searches run in the existing background refresh job, at most **once every 6 hours** (one request with an 8-call web-tool budget). A failed search retries after **1 hour**. Repeated Refresh clicks reuse stored results. Search adds OpenAI tool/token charges; Free Render restarts discard its cache and cooldown along with the database. `USE_DIGEST_WEB_SEARCH=false` disables it. Optional `DIGEST_SEARCH_MODEL` defaults to `REASONING_MODEL` and must support Responses `web_search` and structured output. A missing key or unsupported model produces a handled status message and preserves saved filings. No new environment variables are required.
- Search cards show original disclosure links and a "Search-discovered" label. Date-only disclosures are included when their IST calendar day overlaps the selected window, with that limitation shown on the card. A recovered NSE feed replaces the approximate date with its precise time without duplicating the same document URL. Research remains restricted to its existing primary-document retrieval.
- Live-source failures preserve saved data and display the last refresh time and an outage message. Sample filings appear only when **`USE_LIVE_NSE=false`**, which is explicitly labeled. Samples are never substituted silently in live mode.
- Research section answers have **no time-based expiry**. They remain current until report content, financial evidence, or the configured research model changes. The app checks for missing/stale portfolio sections on startup and every 15 minutes, and queues each company's updates after its scheduled or manual report sync. One dedicated worker prepares all eight regular topics plus **Promised vs. Delivered**, separate from Digest and chat. Previous answers stay visible with their preparation dates while replacements are generated; failed sections retry after 15 minutes without repeating successful work. The Research tab shows progress toward **180/180** prepared sections. Report checks, unchanged index timestamps, and restarts do not regenerate unchanged answers. First-time preparation requires document and model calls; missing source coverage is reported instead of inventing an answer. The five ad-hoc watchlist companies prepare on demand.

To prepare saved reports locally before a demo, stop the running app and run:

```powershell
.\.venv\Scripts\python.exe -m app.pipeline.prepare_research
# Resume just selected companies:
.\.venv\Scripts\python.exe -m app.pipeline.prepare_research TCS INFY
```

This command uses existing reports and financial snapshots; run `app.pipeline.sync_documents` first if sources have not been indexed. Each completed section is saved independently, so reruns resume missing work. Exit code **0** means all selected sections are current; **1** means some need sources or a retry. The deployed app performs this preparation automatically while `AUTO_REFRESH=true`; its persistent disk retains completed work across redeploys. Run the standalone command with the app stopped to avoid generating the same sections in separate processes.

### Deployment acceptance checks

1. Open `/healthz`, `/digest`, and `/alerts?vol_mult=1&price_pct=0.5`. Wait for the initial background refresh to finish. Alerts should show the available stock count and trading date, or an explicit source failure; Digest should distinguish incomplete history from an empty window.
2. Tap Refresh repeatedly. The saved cards should remain visible and the status should show one refresh in progress. After completion, the page should update without another tap.
3. In Research, wait for the portfolio preparation counter to reach **180/180** (or inspect any reported missing coverage). Open a company and expand the topics: completed answers should appear immediately, with source links. Recheck unchanged reports and restart the service: those answers should be reused. A new report or changed financial evidence should update only that company's sections in the background while previous answers remain available.
4. **Restart or redeploy the service**, then reopen all three tabs. Saved prices, filings and Research answers must remain. Wait **20 minutes without traffic**, then revisit: paid compute should remain available and background refresh timestamps should have advanced.
5. Before sending the application link, verify actual cloud feed access and recent dates. Local feed success alone does not prove that Render's IP can reach the same endpoints. An outage should show saved dated results, rather than an unexplained empty page.

Run the automated regression and outage tests locally:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Hugging Face Spaces (Docker SDK) is an alternative deployment target using the same Dockerfile.

## Project layout

```
app/
  main.py          FastAPI app + routes
  config.py        env-driven config, portfolio, alert thresholds
  db.py            SQLite schema + connection helper
  sources/         official NSE RSS/reports, Yahoo, Screener, sample clients
  feeds.py         automatic refresh, deduplication and durable feed health
  pipeline/         classification, digest, alerts, enrichment, Pull-mode analysis
  llm/               OpenAI client wrapper, prompts, response cache
  templates/          Jinja2 + HTMX + Tailwind UI
seed_data/            curated portfolio + fallback announcements/documents
```
