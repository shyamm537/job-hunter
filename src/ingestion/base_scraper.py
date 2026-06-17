"""Abstract base class for job board scrapers (Strategy Pattern).

Each job board gets its own subclass implementing `.scrape()`. The rest of
the pipeline only ever calls that one method — it doesn't know or care how
any given site's HTML or feed is structured. If a site changes its layout,
only that subclass needs to change.
"""

from abc import ABC, abstractmethod
from typing import List

from src.storage.models import JobPost


class BaseScraper(ABC):
    #: Short identifier used in job_board_id and logging, e.g. "seek".
    source_name: str = "unknown"

    @abstractmethod
    def scrape(self) -> List[JobPost]:
        """Fetch postings from the source and return them as JobPost objects.

        Implementations are responsible for their own pagination and for
        respecting the target site's terms of service. Scraping authenticated
        or login-gated pages is out of scope for this project — see the
        README's "Scraping: scope and ethics" section.
        """
        raise NotImplementedError
