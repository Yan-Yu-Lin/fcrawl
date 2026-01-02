"""Google search engine implementation"""

import random
import time
from typing import Optional
from urllib.parse import quote_plus

from .base import SearchEngine, SearchResult, EngineStatus


class GoogleEngine(SearchEngine):
    """Google search engine"""

    name = "google"
    base_url = "https://www.google.com/search"

    # Pagination: start=0, 10, 20, ...
    results_per_page = 10
    pagination_param = "start"
    pagination_start = 0
    pagination_increment = 10

    def build_search_url(self, query: str, page: int = 0) -> str:
        """Build Google search URL"""
        start = page * self.pagination_increment
        return f"{self.base_url}?q={quote_plus(query)}&start={start}"

    def _add_locale_params(self, url: str, locale: str) -> str:
        """Add Google locale parameters (hl=language, gl=country)"""
        parts = locale.split("-")
        lang = parts[0]  # e.g., "ja" from "ja-JP"
        url += f"&hl={lang}"
        if len(parts) > 1:
            country = parts[1]  # e.g., "JP" from "ja-JP"
            url += f"&gl={country}"
        return url

    def search(self, query: str, limit: int, headless: bool = True,
               locale: Optional[str] = None) -> tuple[list[SearchResult], EngineStatus]:
        """
        Perform Google search with proper locale handling.

        Google uses IP geolocation for search results. To override this,
        we visit google.com first and set proper headers/cookies.
        """
        from camoufox.sync_api import Camoufox

        start_time = time.time()
        results = []
        seen_urls = set()
        max_pages = (limit // self.results_per_page) + 1

        # Parse locale
        lang_code = "en"
        region_code = "US"
        if locale:
            parts = locale.split("-")
            lang_code = parts[0]
            if len(parts) > 1:
                region_code = parts[1]

        # Build Camoufox options with locale
        camoufox_opts = {
            "headless": headless,
            "humanize": True,
            "block_images": True,
            "i_know_what_im_doing": True,
            "os": self.os_name,
            "locale": locale or "en-US",
            "config": {
                "headers.Accept-Language": f"{locale or 'en-US'},{lang_code};q=0.9,en;q=0.8",
                "locale:language": lang_code,
                "locale:region": region_code,
            }
        }

        try:
            with Camoufox(**camoufox_opts) as browser:
                page = browser.new_page()

                # Set Google cookies for locale preference
                google_cookies = [
                    {
                        "name": "PREF",
                        "value": f"hl={lang_code}&gl={region_code}",
                        "domain": ".google.com",
                        "path": "/"
                    },
                    {
                        "name": "NID",
                        "value": f"hl={lang_code}",
                        "domain": ".google.com",
                        "path": "/"
                    }
                ]
                page.context.add_cookies(google_cookies)

                # Visit Google homepage first to initialize session
                page.goto("https://www.google.com/", wait_until="domcontentloaded")
                time.sleep(random.uniform(0.3, 0.7))
                self.handle_consent(page)

                for page_num in range(max_pages):
                    if len(results) >= limit:
                        break

                    search_url = self.build_search_url(query, page_num)
                    if locale:
                        search_url = self._add_locale_params(search_url, locale)

                    page.goto(search_url, wait_until="domcontentloaded")
                    time.sleep(random.uniform(0.5, 1.5))

                    if page_num == 0:
                        self.handle_consent(page)

                    page_results = self.extract_results(page)
                    if not page_results:
                        break

                    for r in page_results:
                        if r.url not in seen_urls:
                            seen_urls.add(r.url)
                            r.position = len(results) + 1
                            results.append(r)
                            if len(results) >= limit:
                                break

                    if page_num < max_pages - 1 and len(results) < limit:
                        time.sleep(random.uniform(1.0, 2.0))

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

    def handle_consent(self, page) -> None:
        """Handle Google cookie consent popup"""
        try:
            for selector in [
                "button:has-text('Accept all')",
                "button:has-text('Accept')",
                "button:has-text('I agree')",
                "[aria-label='Accept all']",
            ]:
                consent = page.locator(selector).first
                if consent.is_visible(timeout=1000):
                    consent.click()
                    time.sleep(0.5)
                    break
        except Exception:
            pass

    def extract_results(self, page) -> list[SearchResult]:
        """Extract search results from Google SERP"""
        results = []

        # Google's result structure: div[data-snf='x5WNvb'] contains title/URL
        result_containers = page.locator("div[data-snf='x5WNvb']").all()

        # Fallback to div.yuRUbf if data-snf not found
        if not result_containers:
            result_containers = page.locator("div.yuRUbf").all()

        for elem in result_containers:
            try:
                # Title (h3 inside the result)
                title_elem = elem.locator("h3").first
                title = title_elem.text_content() if title_elem.count() > 0 else ""

                # URL (first anchor link)
                link_elem = elem.locator("a").first
                url = link_elem.get_attribute("href") if link_elem.count() > 0 else ""

                # Description/snippet - in the following sibling element
                description = ""
                sibling = elem.locator("xpath=following-sibling::*[1]")
                if sibling.count() > 0:
                    desc_elem = sibling.locator("div.VwiC3b").first
                    if desc_elem.count() > 0:
                        description = desc_elem.text_content() or ""

                # Fallback: try inside parent container
                if not description:
                    parent = elem.locator("xpath=..").first
                    if parent.count() > 0:
                        desc_elem = parent.locator("div.VwiC3b").first
                        if desc_elem.count() > 0:
                            description = desc_elem.text_content() or ""

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
