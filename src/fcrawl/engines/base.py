"""Base class for search engines"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
import asyncio
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

    def get_context_options(self, locale: Optional[str] = None) -> dict:
        """
        Return Playwright context options (locale, headers).
        Override in subclass for engine-specific settings.
        """
        if not locale:
            return {}
        # Basic locale support
        lang_code = locale.split("-")[0]
        return {
            "locale": locale,
            "extra_http_headers": {
                "Accept-Language": f"{locale},{lang_code};q=0.9,en;q=0.8"
            }
        }

    def setup_context(self, context, locale: Optional[str] = None) -> None:
        """
        Set up cookies/state on the context.
        Override in subclass for engine-specific cookies.
        """
        pass

    def search_with_page(self, page, query: str, limit: int,
                         locale: Optional[str] = None) -> tuple[list[SearchResult], EngineStatus]:
        """
        Search using a provided page (context already set up).
        This is the core search logic without browser creation.

        Args:
            page: Playwright page object (already created from a context)
            query: Search query
            limit: Maximum number of results
            locale: Locale for regional results

        Returns:
            Tuple of (results list, engine status)
        """
        start_time = time.time()
        results = []
        seen_urls = set()
        max_pages = (limit // self.results_per_page) + 1

        try:
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

    async def setup_context_async(self, context, locale: Optional[str] = None) -> None:
        """
        Async version of setup_context.
        Set up cookies/state on the context.
        Override in subclass for engine-specific cookies.
        """
        # Default implementation - add_cookies is sync but should work in async context
        # Subclasses can override if needed
        pass

    async def handle_consent_async(self, page) -> None:
        """
        Async version of handle_consent.
        Handle cookie consent popup if present. Override in subclass.
        """
        pass

    async def search_with_page_async(self, page, query: str, limit: int,
                                     locale: Optional[str] = None) -> tuple[list[SearchResult], EngineStatus]:
        """
        Async version of search_with_page.
        Search using a provided async page (context already set up).

        Args:
            page: Playwright async page object
            query: Search query
            limit: Maximum number of results
            locale: Locale for regional results

        Returns:
            Tuple of (results list, engine status)
        """
        start_time = time.time()
        results = []
        seen_urls = set()
        max_pages = (limit // self.results_per_page) + 1

        try:
            for page_num in range(max_pages):
                if len(results) >= limit:
                    break

                # Build URL and navigate
                search_url = self.build_search_url(query, page_num)
                if locale:
                    search_url = self._add_locale_params(search_url, locale)

                await page.goto(search_url, wait_until="domcontentloaded")

                # Small random delay to appear human
                await asyncio.sleep(random.uniform(0.5, 1.5))

                # Handle consent popup on first page
                if page_num == 0:
                    await self.handle_consent_async(page)

                # Extract results (sync operation on page content)
                page_results = await self.extract_results_async(page)

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
                    await asyncio.sleep(random.uniform(1.0, 2.0))

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

    async def extract_results_async(self, page) -> list[SearchResult]:
        """
        Async version of extract_results.
        Default implementation calls the sync version.
        Override in subclass if async locator operations are needed.
        """
        # Most Playwright locator operations need await in async mode
        # Subclasses should override this
        return self.extract_results(page)

    def search(self, query: str, limit: int, headless: bool = True,
               locale: Optional[str] = None) -> tuple[list[SearchResult], EngineStatus]:
        """
        Perform search and return results with status.
        Creates its own browser - use search_with_page() for shared browser.

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

        # Build Camoufox options
        camoufox_opts = {
            "headless": headless,
            "humanize": True,
            "block_images": True,
            "i_know_what_im_doing": True,
            "os": self.os_name,
            # Fix User-Agent bug in Camoufox v135 (Firefox/v135.0 -> Firefox/135.0)
            "config": {
                "navigator.userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:135.0) Gecko/20100101 Firefox/135.0"
            },
        }
        if locale:
            camoufox_opts["locale"] = locale

        try:
            with Camoufox(**camoufox_opts) as browser:
                # Create context with engine-specific options
                context_opts = self.get_context_options(locale)
                context = browser.new_context(**context_opts)

                # Engine-specific cookie/state setup
                self.setup_context(context, locale)

                # Create page and run search
                page = context.new_page()
                results, status = self.search_with_page(page, query, limit, locale)

                # Adjust timing to include browser setup
                elapsed = time.time() - start_time
                status.elapsed_time = elapsed

                return results, status

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
