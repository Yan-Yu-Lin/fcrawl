"""Base class for search engines"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import platform
import time
import random


@dataclass
class SearchResult:
    """A single search result"""
    title: str
    url: str
    description: str
    engine: str
    position: int  # Position in original search results (1-indexed)

    def to_dict(self) -> dict:
        return {
            'title': self.title,
            'url': self.url,
            'description': self.description,
            'engine': self.engine,
            'position': self.position,
        }


@dataclass
class EngineStatus:
    """Status of a search engine query"""
    engine: str
    success: bool
    result_count: int = 0
    elapsed_time: float = 0.0
    error: Optional[str] = None


class SearchEngine(ABC):
    """Abstract base class for search engines"""

    name: str = "base"
    base_url: str = ""

    # Pagination settings
    results_per_page: int = 10
    pagination_param: str = "start"
    pagination_start: int = 0
    pagination_increment: int = 10

    def __init__(self):
        self.os_name = self._get_os_name()

    @staticmethod
    def _get_os_name() -> str:
        """Get OS name for Camoufox spoofing"""
        system = platform.system().lower()
        if system == "darwin":
            return "macos"
        elif system == "windows":
            return "windows"
        else:
            return "linux"

    @abstractmethod
    def build_search_url(self, query: str, page: int = 0) -> str:
        """Build the search URL for a given query and page number"""
        pass

    @abstractmethod
    def extract_results(self, page) -> list[SearchResult]:
        """Extract search results from the page using Playwright page object"""
        pass

    def handle_consent(self, page) -> None:
        """Handle cookie consent popup if present. Override in subclass."""
        pass

    def search(self, query: str, limit: int, headless: bool = True,
               locale: Optional[str] = None) -> tuple[list[SearchResult], EngineStatus]:
        """
        Perform search and return results with status.

        Args:
            query: Search query
            limit: Maximum number of results
            headless: Run browser in headless mode
            locale: Locale for regional results (e.g., "ja-JP")

        Returns:
            Tuple of (results list, engine status)
        """
        from camoufox.sync_api import Camoufox

        start_time = time.time()
        results = []
        seen_urls = set()
        max_pages = (limit // self.results_per_page) + 1

        # Build Camoufox options
        camoufox_opts = {
            "headless": headless,
            "humanize": True,
            "block_images": True,
            "i_know_what_im_doing": True,
            "os": self.os_name,
        }
        if locale:
            camoufox_opts["locale"] = locale

        try:
            with Camoufox(**camoufox_opts) as browser:
                page = browser.new_page()

                for page_num in range(max_pages):
                    if len(results) >= limit:
                        break

                    # Build URL and navigate
                    search_url = self.build_search_url(query, page_num)
                    if locale:
                        search_url = self._add_locale_params(search_url, locale)

                    page.goto(search_url, wait_until="domcontentloaded")

                    # Small random delay to appear human
                    time.sleep(random.uniform(0.5, 1.5))

                    # Handle consent popup on first page
                    if page_num == 0:
                        self.handle_consent(page)

                    # Extract results
                    page_results = self.extract_results(page)

                    # No more results
                    if not page_results:
                        break

                    # Add unique results with position
                    for r in page_results:
                        if r.url not in seen_urls:
                            seen_urls.add(r.url)
                            # Update position to be global across pages
                            r.position = len(results) + 1
                            results.append(r)
                            if len(results) >= limit:
                                break

                    # Delay between pages
                    if page_num < max_pages - 1 and len(results) < limit:
                        time.sleep(random.uniform(1.0, 2.0))

            elapsed = time.time() - start_time
            status = EngineStatus(
                engine=self.name,
                success=True,
                result_count=len(results),
                elapsed_time=elapsed
            )
            return results[:limit], status

        except Exception as e:
            elapsed = time.time() - start_time
            status = EngineStatus(
                engine=self.name,
                success=False,
                result_count=0,
                elapsed_time=elapsed,
                error=str(e)
            )
            return [], status

    def _add_locale_params(self, url: str, locale: str) -> str:
        """Add locale parameters to URL. Override in subclass if needed."""
        return url
