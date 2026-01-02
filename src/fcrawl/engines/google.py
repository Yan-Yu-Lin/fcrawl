"""Google search engine implementation"""

import time
from urllib.parse import quote_plus

from .base import SearchEngine, SearchResult


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
