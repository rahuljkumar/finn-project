# FINN — Financial Investment Agent

A mobile-first portfolio intelligence agent with two modes:

- **Push** — a windowed digest of portfolio filings/announcements, filtered by priority, plus triggered alerts on material events or unusual price/volume moves.
- **Pull** — on-demand company research grounded only in primary sources (annual reports, concall transcripts): business snapshot, financial snapshot, trajectory, balance sheet, cash quality, narrative vs. numbers, guidance, and bull vs. bear — including a "promised vs. delivered" view across the last four quarters.

## Stack

Single FastAPI service (Python) rendering server-side Jinja2 templates with HTMX for partial updates, Tailwind (CDN) for styling, and Alpine.js for light client-side interactivity. Data: `yfinance` for EOD price/volume, NSE/BSE public endpoints for announcements, `screener.in` company pages for fundamentals and document links, with a curated `seed_data/` fallback so the demo doesn't depend on live scraping succeeding.

## Local setup

```bash
python -m venv .venv
.venv/Scripts/activate       # or `source .venv/bin/activate` on macOS/Linux
pip install -r requirements.txt
cp .env.example .env         # then fill in OPENAI_API_KEY
uvicorn app.main:app --reload
```

Visit `http://127.0.0.1:8000`.

## Deployment

Deploy as a Docker web service on Render's free tier. Push the project to a private GitHub repository, then create a Render Blueprint using the included `render.yaml`. Set `OPENAI_API_KEY`, `CHEAP_MODEL`, and `REASONING_MODEL` in Render to the values used by your local environment. The Blueprint selects the Free plan and `/healthz` health check; the Dockerfile binds to Render's `$PORT`.

For a manual Web Service setup, select Docker, Free, `./Dockerfile`, and health check path `/healthz`. Add the same environment variables plus `EMBEDDING_MODEL=text-embedding-3-small` and `DB_PATH=/srv/data/finn.db`.

After deployment, open `/healthz`, `/digest`, `/alerts`, and `/research`. Refresh Digest and Alerts to populate the hosted database, then test a research section. Free instances sleep after 15 minutes of inactivity and discard SQLite data and caches on sleep, restart, or redeploy. Refresh again when needed; repeated research may make new model calls after a reset. Do not commit `.env` or the challenge PDFs. `.dockerignore` also excludes local secrets and cached data from the image.

Hugging Face Spaces (Docker SDK) is an alternative deployment target using the same Dockerfile.

## Project layout

```
app/
  main.py          FastAPI app + routes
  config.py        env-driven config, portfolio, alert thresholds
  db.py            SQLite schema + connection helper
  sources/         yfinance / NSE / BSE / screener.in / seed-data clients
  pipeline/         classification, digest, alerts, enrichment, Pull-mode analysis
  llm/               OpenAI client wrapper, prompts, response cache
  templates/          Jinja2 + HTMX + Tailwind UI
seed_data/            curated portfolio + fallback announcements/documents
```
