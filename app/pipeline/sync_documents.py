"""Run the same incremental sync used by the EOD background refresh.

Usage: python -m app.pipeline.sync_documents [TICKER ...]
"""

import json
import sys

from app.db import init_db
from app.pipeline.pull_pipeline import sync_portfolio


if __name__ == "__main__":
    init_db()
    print(json.dumps(sync_portfolio(sys.argv[1:] or None), indent=2))
