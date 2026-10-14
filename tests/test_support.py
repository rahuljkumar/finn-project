from unittest.mock import patch


def isolate_background(test):
    """Existing HTTP tests must not start newly added paid/network background work."""
    def disabled(key, kind, work, **kwargs):
        return {"job_key": key, "kind": kind, "state": "disabled", "result": None,
                "error": "Background work isolated by the test fixture."}
    for target in ("app.main.start_job", "app.pipeline.news.start_job",
                   "app.pipeline.summaries.start_job", "app.pipeline.conversation.start_job"):
        test_patch = patch(target, side_effect=disabled)
        test_patch.start()
        test.addCleanup(test_patch.stop)
