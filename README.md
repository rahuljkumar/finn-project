# FINN — Financial Investment Agent

A mobile-first portfolio intelligence agent with two modes:

- **Push** — a windowed digest of portfolio filings/announcements, filtered by priority, plus triggered alerts on material events or unusual price/volume moves.
- **Pull** — on-demand company research grounded only in primary sources (annual reports, concall transcripts): business snapshot, financial snapshot, trajectory, balance sheet, cash quality, narrative vs. numbers, guidance, and bull vs. bear — including a "promised vs. delivered" view across the last four quarters.

## Stack

Single FastAPI service (Python) rendering server-side Jinja2 templates with HTMX for partial updates, Tailwind (CDN) for styling, and Alpine.js for light client-side interactivity. Data: official NSE daily reports for EOD price/volume, Yahoo Finance as a secondary price source, official NSE RSS for recent announcements, the NSE JSON endpoint for historical backfill, and `screener.in` company pages for fundamentals and document links. No market-data API key is required for these feeds. BSE ingestion is not implemented.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/activate       # or `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
cp .env.example .env         # PowerShell: Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000`.

## Deployment

Deploy as a Docker web service on Render's free tier. Push the project to a private GitHub repository, then create a Render Blueprint using the included `render.yaml`. Set `OPENAI_API_KEY`, `CHEAP_MODEL`, and `REASONING_MODEL` in Render to the values used by your local environment. The Blueprint selects the Free plan and `/healthz` health check; the Dockerfile binds to Render's `$PORT`.

For a manual Web Service setup, select Docker, Free, `./Dockerfile`, and health check path `/healthz`. Add the same environment variables plus `EMBEDDING_MODEL=text-embedding-3-small` and `DB_PATH=/srv/data/finn.db`.

The Free plan is useful for trying the app, but does not provide continuous operation or durable local storage. Free instances sleep after 15 minutes of inactivity and discard SQLite data and caches on sleep, restart, or redeploy. A paid instance also needs a persistent disk to preserve its files. Do not commit `.env` or the challenge PDFs. `.dockerignore` excludes local secrets and cached data from the image.

### Continuous demo on Render

For this single-instance app, choose the $7/month compute service and attach a 1 GB persistent disk (currently $0.25/GB/month). A paid workspace subscription or separate database service is not required. Confirm the current prices on [Render's pricing page](https://render.com/pricing).

1. In the existing service's **Settings**, select the $7 compute instance.
2. Open **Disks**, add a disk of **1 GB**, and set its mount path to **`/srv/data`**. Adding a disk triggers a deploy. Only files under that mount path persist; see [Render's disk documentation](https://render.com/docs/disks).
3. Keep **`DB_PATH=/srv/data/finn.db`**, **`USE_LIVE_NSE=true`**, and **`AUTO_REFRESH=true`** in the service's environment. Keep the existing OpenAI key and model settings. All database, PDF, vector-index and report caches now use the database's parent directory.
4. Deploy the latest GitHub commit. The first deploy with a new disk starts with an empty database; local laptop data is not copied automatically.

The included `render.yaml` still selects Free for new Blueprint deployments. For an existing manually created service, configure paid compute and the disk in the dashboard. Keep one service instance and one Uvicorn worker: the refresh coordinator and research memory limit are process-local. A disk-backed service has a brief interruption during redeployment.

### Freshness and outage behavior

- On startup, background workers begin loading both feeds. Filings are polled every **5 minutes**, prices every **30 minutes** while the service is running. `AUTO_REFRESH=false` disables scheduled polling; manual Refresh still works.
- Refresh returns the page immediately and retains saved results while fetching. The page polls until the job finishes. Repeated clicks reuse the running job and have a 60-second cooldown.
- Prices are **end-of-day**, not streaming quotes. The official CSV source loads about 45 calendar days initially, then catches up from saved history. Its reported previous close determines each daily price change; a full 20-session volume average is required for volume alerts. Missing/blocked reports fall back to Yahoo. Both sources can still become unavailable.
- RSS contains recent announcements, usually a short window. The app retains them over time and attempts a 120-day JSON backfill once a day until it succeeds. The UI labels incomplete historical coverage. A fresh deployment cannot promise a complete 7-day or 30-day digest when historical access is blocked.
- Live-source failures preserve saved data and display the last refresh time and an outage message. Sample filings appear only when **`USE_LIVE_NSE=false`**, which is explicitly labeled. Samples are never substituted silently in live mode.
- Successful Research answers are saved for **6 hours**. Expired answers are regenerated on demand; a temporary failure serves the last saved answer with its preparation date and a notice. Document links are checked daily when an index is used. First-time research still needs successful document and model requests, and can take a few minutes to build an index. One new Research request is processed at a time to bound RAM use; cached answers remain fast.

### Deployment acceptance checks

1. Open `/healthz`, `/digest`, and `/alerts?vol_mult=1&price_pct=0.5`. Wait for the initial background refresh to finish. Alerts should show the available stock count and trading date, or an explicit source failure; Digest should distinguish incomplete history from an empty window.
2. Tap Refresh repeatedly. The saved cards should remain visible and the status should show one refresh in progress. After completion, the page should update without another tap.
3. Open a company's Research section, wait for its first answer, and verify its source links. Reopen that same section: the saved answer should load quickly.
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
