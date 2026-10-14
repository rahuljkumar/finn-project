# FINN: assignment upgrade plan

Status: core implementation complete, 14 October 2026. This plan records the agreed local-demo behavior. Run commands are in [README.md](../README.md).

## Agreed scope

Use the existing OpenAI API key, Screener company-page scraper, SQLite database, local PDF cache, and persisted JSON/NumPy vector index. Azure and a database migration are not required for this assignment.

The demo will run on this machine. Render Free remains the hosted preview; hosting changes and production infrastructure are outside this iteration.

Focus the work on three investor needs:

- Research: ask follow-up questions about company reports and inspect the supporting evidence.
- Alerts: understand possible catalysts behind material price or volume changes.
- Digest: understand significant disclosures quickly without opening every PDF.

## Confirmed product rules

- Check watched-company annual reports and concall transcripts daily at EOD.
- Download, chunk, and embed only newly discovered reports; unchanged reports require no new document embeddings.
- Add a ticker-scoped conversation at the bottom of the Research company screen.
- Support natural-language questions across all nine existing research topics.
- Suppress all Alert news searches if either selected filter is below 2x volume or 5% price.
- When both selected filters meet their floors, either actual metric exceeding its selected threshold can trigger a search.
- Alerts contains price/volume moves and their explanations; company announcements and their summaries belong in Digest.
- Combine priority and notable Digest updates into one summary per ticker for the current view.
- Automatically prepare ticker summaries for standard 24h, 7d, and 30d presets.
- Custom windows or additional filters use a manual Generate AI summary button.
- Routine updates use a manual summary button per article, with the result beside that article.

## Research: reports ready before the user asks

### Document ingestion

Report discovery runs separately from retrieval through the existing background refresh setup. EOD check time: 18:30 IST, configurable. The local schedule runs while the app and machine are running, and catches up on startup.

A small document manifest records ticker, report type, report label/period, URL, content hash, processing status, and check time.

Daily flow:

1. Fetch each watched ticker's existing Screener company page.
2. Discover annual-report and concall-transcript links.
3. Compare them against the manifest.
4. Download new reports, extract their text with page references, and chunk them.
5. Embed only the new chunks using the existing OpenAI embedding client.
6. Append them to the stored ticker index and save the updated manifest.
7. Refresh the stored financial snapshot and invalidate affected research answers.
8. Show the last successful check and available report periods.

Use content hashes to avoid processing an identical PDF discovered under a different URL. Preserve existing indexed reports if a new download or model call fails. Record unreadable PDFs as unavailable rather than answering from empty text.

Retain the current one-annual-report/eight-concall coverage initially. Expand history only when a chosen research question needs it. Preserve enough historical calls for Promised vs. Delivered.

Add a local action to run the same document-sync function immediately for preparation and demonstration. It should expose checked, new, unchanged, indexed, and failed counts so the recording can demonstrate an unchanged second run without waiting for the next EOD.

For a new ticker with no stored reports, start background preparation and show its status instead of blocking the page on downloads and embeddings.

### Conversation

Place an Ask about this company conversation below the nine sections.

Question -> resolve ticker, follow-up context, and requested period -> retrieve report excerpts and financial tables -> calculate figures where needed -> OpenAI answer with citations.

Suggested starter questions:

- What are the main growth drivers?
- Has cash generation kept up with profit?
- What has management guided on?
- Did management deliver on its previous guidance?

Keep source references visible beside the answer. Users should be able to open the cited report and identify the relevant pages. When reports do not support an answer, say what information is missing.

Follow-ups should retain the company and subject while retrieving supporting evidence again. Previous generated answers are conversational context, not source evidence.

| Existing topic | Answer approach |
| --- | --- |
| Business Snapshot | Retrieve business/segment descriptions |
| Financial Snapshot | Stored financial tables plus supporting commentary |
| Trajectory | Compare historical financial periods |
| Balance Sheet | Debt, funding, and working-capital evidence |
| Cash Quality | Cash-flow evidence and application-side calculations |
| Narrative vs. Numbers | Align commentary with the corresponding results |
| Guidance | Retrieve explicit dated management statements |
| Bull vs. Bear | Summarize supported opportunities and risks |
| Promised vs. Delivered | Reuse quarter matching, historical guidance retrieval, and actual results |

The existing section cards and conversation should share the same stored evidence. A new document or financial snapshot should invalidate relevant cached answers.

## Alerts: explain material moves

Use the confirmed two-stage rule:

```text
search_enabled = volume_filter >= 2 AND price_filter >= 5

stock_qualifies =
    actual_volume_ratio > volume_filter
    OR abs(actual_price_move_pct) > price_filter

search = search_enabled AND stock_qualifies
```

Exactly 2x and 5% selected filters enable searches. Actual-stock search comparisons use strictly greater than the selected thresholds. The existing price-alert display retains its inclusive boundary; a stock exactly at the selected boundary does not start a news search unless the other metric exceeds its threshold.

| Filters | Actual activity | Search |
| --- | --- | --- |
| 1.5x / 6% | Any move | No |
| 3x / 4% | Any move | No |
| 2x / 5% | Volume 3x, price +2% | Yes |
| 2x / 5% | Volume 1.3x, price -6% | Yes |
| 3x / 6% | Volume 2.5x, price +5.5% | No |

For each qualifying stock, use the existing OpenAI Responses web-search capability with a supported model. Search using company name, ticker, and the actual trading date on the alert. Retain article URLs, publication dates, and supporting excerpts.

Add a short explanation beneath the existing price/volume information, with two or three useful source links. Distinguish:

- Reported catalyst: a source explicitly links an event to the move.
- Possibly related news: relevant coverage exists without a clear causal link.
- No reliable explanation found: the search completed without supporting evidence.

A provider failure should appear as a retryable error rather than a no-news result.

Cache explanations by ticker and trading date, including a fingerprint of the price data. Repeated views reuse evidence. Low-filter selections create no searches; newly qualifying filter selections can start background searches.

## Digest: one useful summary per ticker

### Priority and notable updates

Read the actual attachment or verified article contents before summarizing. Extract reusable facts, including event dates, amounts, financial figures, parties, and people. Cache those facts using the disclosure identity, URL, metadata, and model, retaining its document content hash.

For the selected window, combine matching priority and notable facts into one concise ticker summary. Preserve distinct events and dates; avoid counting duplicate disclosures twice.

Recommended card:

- Company/ticker and priority/notable counts.
- One combined AI summary.
- Source citations.
- Expandable supporting filings grouped into Priority and Notable.

Order cards by their highest contained priority, then recency. Keep routine updates in the existing collapsed area.

### Management changes

Extract names and roles from the filing and its annexures, rather than only its headline. Include the effective date, professional background, and previous companies where supported.

Use the filing's biography first. If background is missing, use verified public sources with an exact name/company/role match and source links. Clearly identify unavailable background.

### Automatic versus manual

| Selection | Priority + notable | Routine |
| --- | --- | --- |
| Standard 24h | Automatic per-ticker summary | Manual per article |
| Standard 7d | Automatic per-ticker summary | Manual per article |
| Standard 30d | Automatic per-ticker summary | Manual per article |
| Custom hours/range or extra filters | Manual Generate AI summary | Manual per article |

Switching between standard presets remains automatic. Custom generation uses only the articles matching the submitted filters.

Prepare automatic summaries after filing refresh and reuse them until their evidence set changes. Reconcile rolling windows so articles aging out also change the relevant summary. Avoid regenerating unchanged summaries on every visit.

Store a job key incorporating ticker, filter scope, contributing article IDs/metadata, and models, alongside the summary, citations, and generation time. Extracted facts retain document content hashes. A summary for one selection must not be displayed as a summary for another.

Routine attachment extraction and summarization happen when the user requests that article. Display its summary inline and cache it for later clicks.

Show loading, ready, partial-source, and retry states clearly. Keep the pages usable while OpenAI calls run.

## Build order

| Step | Product deliverable |
| --- | --- |
| 1 | Incremental report ingestion, document manifest, stored financial snapshots, and a local sync action |
| 2 | Research conversation with follow-ups, all nine topics, and clickable citations |
| 3 | Alert threshold gate, background news search, short explanation, and source links |
| 4 | Digest document facts, management details, combined ticker summaries, preset behavior, and routine buttons |
| 5 | Demo preparation and representative end-to-end checks |

Reuse the current FastAPI/Jinja/HTMX UI and background threads. Persist manifests and successful outputs locally. A small in-flight guard and cached statuses are enough for this single-process demo.

Main code areas:

- Research: `pipeline/documents.py`, `pipeline/pull_pipeline.py`, `pipeline/research_cache.py`, `pipeline/promised_vs_delivered.py`, `feeds.py`, `templates/company.html`.
- Alerts: `pipeline/alerts.py`, structured numeric anomaly fields, and `templates/alerts.html`.
- Digest: `pipeline/digest.py`, `pipeline/enrich.py`, `templates/digest.html`, and `templates/partials/announcement_card.html`.
- Shared: extend the existing SQLite schema and OpenAI client/cache only where the features require it.

## Demo storyline

1. Prepare real reports and disclosures before recording; retain their original source links and dates.
2. Run document sync and show that an unchanged second run adds no embeddings.
3. Ask a Research question, follow up naturally, and open its supporting citation.
4. Ask a question with insufficient report evidence and show the explicit limitation.
5. Open a qualifying Alert and inspect the sourced explanation.
6. Lower one Alert filter below its floor and show that search is disabled.
7. Show a ticker with priority and notable filings together under one summary.
8. Switch between automatic presets, then use a custom window and its manual button.
9. Generate a routine article summary and, where available, show a management change with sourced background.

If there is no genuine new report or suitable price anomaly during recording, use a clearly labelled historical example or controlled replay of real stored source data. Do not present it as a new live event.

## Checks that matter for the assignment

- Unchanged document sync performs zero new document embedding calls.
- One new report processes only its new chunks.
- All nine research topics work with meaningful evidence and valid citations.
- Follow-ups retain context, and unsupported questions do not invent answers.
- Either low Alert filter produces zero searches; either qualifying metric can trigger an enabled search.
- Repeated Alert views reuse the same event explanation.
- Each ticker has one combined priority/notable summary for the current selection.
- Presets are automatic; custom selections are manual and exclude filtered-out facts.
- Management details come from actual sources.
- Routine updates receive no automatic summaries.
- Loading and failed calls do not prevent users from navigating the tabs.

## Implementation verification

The automated suite passes **72 tests**, covering incremental ingestion, stored-only retrieval, citations and follow-up context, financial comparisons, Alert gates and cache reuse, combined ticker summaries, preset/manual scope, management extraction, and failure states.

Live checks with the configured OpenAI client produced a cited TCS Research answer, a sourced explanation for TCS's 13 October move, and a Sun Pharma disclosure summary with management background. A repeated TCS document check reused eight reports and made **zero document embedding calls**. One additional transcript returned HTTP 403 and was recorded as failed without removing saved reports. The Sun Pharma summary explicitly reported partial coverage because one attachment could not be read.

Local HTTP rendering checks passed. Browser visual inspection remains outstanding because the browser automation helper could not start in this environment.

## References

The existing OpenAI key is the intended credential for this implementation. Generation, embeddings, and supported-model web search are direct OpenAI capabilities:

- [Official OpenAI quickstart](https://developers.openai.com/api/docs/quickstart)
- [Official OpenAI embeddings guidance](https://developers.openai.com/api/docs/guides/embeddings)
- [Official OpenAI web-search guidance](https://developers.openai.com/api/docs/guides/tools-web-search)
