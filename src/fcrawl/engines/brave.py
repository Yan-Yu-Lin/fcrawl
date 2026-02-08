"""Brave search engine implementation"""

import asyncio
import time
from typing import Optional
from urllib.parse import quote_plus

from .base import SearchEngine, SearchResult, EngineStatus


class BraveEngine(SearchEngine):
    """Brave search engine"""

    name = "brave"
    base_url = "https://search.brave.com/search"

    # Pagination: offset=0, 10, 20, ...
    results_per_page = 10
    pagination_param = "offset"
    pagination_start = 0
    pagination_increment = 10

    def build_search_url(self, query: str, page: int = 0) -> str:
        """Build Brave search URL"""
        offset = page * self.pagination_increment
        return f"{self.base_url}?q={quote_plus(query)}&offset={offset}"

    def _add_locale_params(self, url: str, locale: str) -> str:
        """Add Brave locale parameters"""
        parts = locale.split("-")
        if len(parts) > 1:
            country = parts[1].lower()
            url += f"&country={country}"
        return url

    def get_context_options(self, locale: Optional[str] = None) -> dict:
        """Return context options with Brave-specific headers"""
        lang_code = "en"
        if locale:
            lang_code = locale.split("-")[0]
        return {
            "locale": locale or "en-US",
            "extra_http_headers": {
                "Accept-Language": f"{locale or 'en-US'},{lang_code};q=0.9,en;q=0.8"
            }
        }

    def setup_context(self, context, locale: Optional[str] = None) -> None:
        """Brave doesn't require special cookies for locale handling"""
        # Brave Search respects Accept-Language header, no cookies needed
        pass

    def handle_consent(self, page) -> None:
        """Handle Brave consent/popup if any"""
        # Brave typically doesn't have intrusive consent popups
        # but we handle potential cases
        try:
            for selector in [
                "button:has-text('Accept')",
                "button:has-text('Got it')",
                "[data-action='accept']",
            ]:
                consent = page.locator(selector).first
                if consent.is_visible(timeout=1000):
                    consent.click()
                    time.sleep(0.5)
                    break
        except Exception:
            pass

    def extract_results(self, page) -> list[SearchResult]:
        """Extract search results from Brave SERP"""
        results = []

        # Brave's result structure (2024+):
        # div.snippet[data-type='web'] contains each organic result
        # Inside: .result-content > a (with href) > .title (text)
        result_containers = page.locator("div.snippet[data-type='web']").all()

        for elem in result_containers:
            try:
                title = ""
                url = ""
                description = ""

                # URL and title from the main link
                # Structure: .result-content > a.l1[href] > .title
                link_elem = elem.locator("a[href^='http']").first
                if link_elem.count() > 0:
                    url = link_elem.get_attribute("href") or ""

                    # Title is inside the link
                    title_elem = link_elem.locator(".title").first
                    if title_elem.count() > 0:
                        title = title_elem.text_content() or ""

                # If title not found, try alternative selectors
                if not title:
                    for title_selector in [
                        ".search-snippet-title",
                        ".title",
                        "h2",
                    ]:
                        title_elem = elem.locator(title_selector).first
                        if title_elem.count() > 0:
                            title = title_elem.text_content() or ""
                            if title:
                                break

                # Description - look for snippet description element
                for desc_selector in [
                    ".snippet-description",
                    ".generic-snippet",
                    "p.snippet-content",
                    ".snippet-content",
                ]:
                    desc_elem = elem.locator(desc_selector).first
                    if desc_elem.count() > 0:
                        description = desc_elem.text_content() or ""
                        if description:
                            break

                # Only add if we have a valid URL
                if url and url.startswith("http"):
                    results.append(SearchResult(
                        title=title.strip() if title else "",
                        url=url,
                        description=description.strip() if description else "",
                        engine=self.name,
                        position=len(results) + 1
                    ))
            except Exception:
                continue

        return results

    async def handle_consent_async(self, page) -> None:
        """Async version: Handle Brave consent/popup if any"""
        try:
            for selector in [
                "button:has-text('Accept')",
                "button:has-text('Got it')",
                "[data-action='accept']",
            ]:
                consent = page.locator(selector).first
                if await consent.is_visible(timeout=1000):
                    await consent.click()
                    await asyncio.sleep(0.5)
                    break
        except Exception:
            pass

    async def search_with_page_async(self, page, query: str, limit: int,
                                     locale: Optional[str] = None) -> tuple[list[SearchResult], EngineStatus]:
        """
        Async version: Search using a provided page (context already set up).
        Uses base class logic since Brave doesn't need special initialization.
        """
        import random
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

                # Extract results
                page_results = await self.extract_results_async(page)

                # No more results
                if not page_results:
                    break

                # Add unique results with position
                for r in page_results:
                    if r.url not in seen_urls:
                        seen_urls.add(r.url)
                        r.position = len(results) + 1
                        results.append(r)
                        if len(results) >= limit:
                            break

                # Delay between pages
                if page_num < max_pages - 1 and len(results) < limit:
                    await asyncio.sleep(random.uniform(1.0, 2.0))

            elapsed = time.time() - start_time
            return results[:limit], EngineStatus(
                engine=self.name, success=True,
                result_count=len(results), elapsed_time=elapsed
            )

        except Exception as e:
            elapsed = time.time() - start_time
            return [], EngineStatus(
                engine=self.name, success=False,
                result_count=0, elapsed_time=elapsed, error=str(e)
            )

    async def extract_results_async(self, page) -> list[SearchResult]:
        """Async version: Extract search results from Brave SERP"""
        results = []

        # Brave's result structure (2024+):
        # div.snippet[data-type='web'] contains each organic result
        result_containers = await page.locator("div.snippet[data-type='web']").all()

        for elem in result_containers:
            try:
                title = ""
                url = ""
                description = ""

                # URL and title from the main link
                link_elem = elem.locator("a[href^='http']").first
                if await link_elem.count() > 0:
                    url = await link_elem.get_attribute("href") or ""

                    # Title is inside the link
                    title_elem = link_elem.locator(".title").first
                    if await title_elem.count() > 0:
                        title = await title_elem.text_content() or ""

                # If title not found, try alternative selectors
                if not title:
                    for title_selector in [
                        ".search-snippet-title",
                        ".title",
                        "h2",
                    ]:
                        title_elem = elem.locator(title_selector).first
                        if await title_elem.count() > 0:
                            title = await title_elem.text_content() or ""
                            if title:
                                break

                # Description - look for snippet description element
                for desc_selector in [
                    ".snippet-description",
                    ".generic-snippet",
                    "p.snippet-content",
                    ".snippet-content",
                ]:
                    desc_elem = elem.locator(desc_selector).first
                    if await desc_elem.count() > 0:
                        description = await desc_elem.text_content() or ""
                        if description:
                            break

                # Only add if we have a valid URL
                if url and url.startswith("http"):
                    results.append(SearchResult(
                        title=title.strip() if title else "",
                        url=url,
                        description=description.strip() if description else "",
                        engine=self.name,
                        position=len(results) + 1
                    ))
            except Exception:
                continue

        return results
