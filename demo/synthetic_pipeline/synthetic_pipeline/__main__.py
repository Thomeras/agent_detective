"""CLI: run the synthetic pipeline and export its OTLP traces.

Configuration is entirely environment-driven (see ``config.Settings``):

    INGEST_URL           default http://localhost:8001 (empty => dry run)
    LLM_BASE_URL         default http://localhost:8080/v1 (the mock LLM)
    SCRAPER_HALLUCINATE  "1"/"true" => flagship silent-hallucination fault
    GRAPH_ID             correlation id (empty => random)
    DETERMINISTIC        "1" => fixed ids/timestamps (for fixtures)
    CAPTURE_FILE         write payload to this path instead of POSTing
"""

from __future__ import annotations

import logging

from .config import Settings
from .pipeline import build_and_run


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    build_and_run(Settings())


if __name__ == "__main__":
    main()
