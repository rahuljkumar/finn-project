from unittest.mock import patch


def isolate_background(test):
    """Existing HTTP tests must not start newly added paid/network background work."""
    def disabled(key, kind, work, **kwargs):
        return {"job_key": key, "kind": kind, "state": "disabled", "result": None,
                "error": "Background work isolated by the test fixture."}
    for target in ("app.main.start_job", "app.pipeline.news.start_job",
                   "app.pipeline.summaries.start_job", "app.pipeline.conversation.start_job",
                   "app.pipeline.prepare_research.start_job"):
        test_patch = patch(target, side_effect=disabled)
        test_patch.start()
        test.addCleanup(test_patch.stop)
    # Maintenance is exercised explicitly in its own event-based tests.
    # Existing HTTP/feed tests must not launch historical downloads or AI.
    from app import feeds
    request_refresh = feeds.request_refresh
    maintenance = patch("app.feeds.request_refresh", side_effect=lambda feed, *args, **kwargs:
                        False if feed in {"filings_backfill", "filings_classify"}
                        else request_refresh(feed, *args, **kwargs))
    maintenance.start()
    test.addCleanup(maintenance.stop)
