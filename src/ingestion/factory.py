"""Deprecated shim. Scraper selection moved to src/ingestion/planner.py when
sources and filters were split. Import `plan_scrapes` / `PlannedScrape` from
planner instead. (This file is kept only because it can't be removed here;
`git rm src/ingestion/factory.py` to drop it for good.)
"""

from src.ingestion.planner import PlannedScrape, plan_scrapes  # noqa: F401
