"""Bing search engine implementation"""

import base64
import time
from urllib.parse import quote_plus, urlparse, parse_qs

from .base import SearchEngine, SearchResult


class BingEngine(SearchEngine):
    """Bing search engine"""

    name = "bing"
    base_url = "https://www.bing.com/search"

    # Pagination: first=1, 11, 21, ... (Bing starts at 1, not 0)
    results_per_page = 10
    pagination_param = "first"
    pagination_start = 1
    pagination_increment = 10

    def build_search_url(self, query: str, page: int = 0) -> str:
        """Build Bing search URL"""
        # Bing uses first=1, 11, 21, etc.
        first = (page * self.pagination_increment) + 1
        return f"{self.base_url}?q={quote_plus(query)}&first={first}"

    def _add_locale_params(self, url: str, locale: str) -> str:
        """Add Bing locale parameters (setlang and mkt)"""
        parts = locale.split("-")
        lang = parts[0]
        url += f"&setlang={lang}"
        if len(parts) > 1:
            # Market code like en-US, ja-JP
            url += f"&mkt={locale}"
        return url

    def handle_consent(self, page) -> None:
        """Handle Bing cookie consent popup"""
        try:
            for selector in [
                "button#bnp_btn_accept",
                "button:has-text('Accept')",
                "button:has-text('Agree')",
                "#bnp_container button",
            ]:
                consent = page.locator(selector).first
                if consent.is_visible(timeout=1000):
                    consent.click()
                    time.sleep(0.5)
                    break
        except Exception:
            pass

    def _decode_bing_url(self, url: str) -> str:
        """
        Decode Bing tracking URL to get the actual destination URL.

        Bing wraps URLs like:
        https://www.bing.com/ck/a?...&u=a1<base64_encoded_url>&...

        The 'u' parameter contains 'a1' prefix followed by base64-encoded URL.
        """
        if not url or "bing.com/ck/a" not in url:
            return url

        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)

            if 'u' in params:
                encoded = params['u'][0]
                # Remove 'a1' prefix (Bing's encoding marker)
                if encoded.startswith('a1'):
                    b64_str = encoded[2:]
                    # Add padding if needed
                    padding = 4 - len(b64_str) % 4
                    if padding != 4:
                        b64_str += '=' * padding
                    return base64.b64decode(b64_str).decode('utf-8')
        except Exception:
            pass

        return url

    def extract_results(self, page) -> list[SearchResult]:
        """Extract search results from Bing SERP"""
        results = []

        # Bing's result structure: li.b_algo contains each result
        result_containers = page.locator("li.b_algo").all()

        for elem in result_containers:
            try:
                # Title and URL from h2 > a
                title_link = elem.locator("h2 a").first
                title = ""
                url = ""

                if title_link.count() > 0:
                    title = title_link.text_content() or ""
                    raw_url = title_link.get_attribute("href") or ""
                    # Decode Bing tracking URL
                    url = self._decode_bing_url(raw_url)

                # Description from caption paragraph
                description = ""
                # Try various Bing description selectors
                for desc_selector in [
                    "div.b_caption p",
                    "p.b_lineclamp2",
                    "p.b_algoSlug",
                    "div.b_caption",
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
