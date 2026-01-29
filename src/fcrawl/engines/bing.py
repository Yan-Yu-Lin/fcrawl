"""Bing search engine implementation"""

import base64
import random
import time
from typing import Optional
from urllib.parse import quote_plus, urlparse, parse_qs

from .base import SearchEngine, SearchResult, EngineStatus


class BingEngine(SearchEngine):
    """Bing search engine"""

    name = "bing"
    base_url = "https://www.bing.com/search"

    # Pagination: first=1, 11, 21, ... (Bing starts at 1, not 0)
    results_per_page = 10
    pagination_param = "first"
    pagination_start = 1
    pagination_increment = 10

    def __init__(self):
        super().__init__()
        # Generate cvid once per session for consistency
        self._cvid = self._generate_cvid()

    @staticmethod
    def _generate_cvid() -> str:
        """Generate a Bing conversation ID (32-char hex string)"""
        return ''.join(f'{random.randint(0, 255):02X}' for _ in range(16))

    def build_search_url(self, query: str, page: int = 0) -> str:
        """Build Bing search URL with proper parameters to avoid anti-bot measures"""
        # Bing uses first=1, 11, 21, etc.
        first = (page * self.pagination_increment) + 1

        # Build URL with all necessary Bing parameters
        # cvid = conversation ID, required for proper session handling
        # pq = partial query (same as q for full queries)
        # filters=rcrse:"1" prevents autocorrection that causes wrong results
        # FORM=PERE is the standard form code for web search
        params = [
            f"q={quote_plus(query)}",
            f"pq={quote_plus(query)}",
            f"cvid={self._cvid}",
            f"first={first}",
            'filters=rcrse%3A"1"',  # Disable autocorrect (URL-encoded)
            "FORM=PERE",
            "ghc=1",
            "lq=0",
            "qs=n",
            "sk=",
            "sp=-1",
        ]
        return f"{self.base_url}?{'&'.join(params)}"

    def _add_locale_params(self, url: str, locale: str) -> str:
        """Add Bing locale parameters (setlang, mkt, cc)"""
        parts = locale.split("-")
        lang = parts[0]

        # Handle special language codes
        # Bing doesn't work well with 'zh-hans', convert to 'zh-cn'
        if lang == "zh" and len(parts) > 1:
            region = parts[1].lower()
            if region in ("hans", "cn"):
                lang = "zh-cn"
            elif region in ("hant", "tw", "hk"):
                lang = "zh-tw"

        url += f"&setlang={lang}"
        if len(parts) > 1:
            # Market code like en-US, ja-JP
            url += f"&mkt={locale}"
            # Also add country code for additional locale specificity
            url += f"&cc={parts[1]}"
        return url

    def get_context_options(self, locale: Optional[str] = None) -> dict:
        """Return context options with Bing-specific headers"""
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
        """Set Bing-specific cookies for locale/session handling"""
        # Generate new cvid for this context
        self._cvid = self._generate_cvid()

        lang_code = "en"
        if locale:
            lang_code = locale.split("-")[0]

        bing_cookies = [
            {
                "name": "_EDGE_S",
                "value": f"mkt={locale or 'en-US'}&ui={lang_code}",
                "domain": ".bing.com",
                "path": "/"
            },
            {
                "name": "SRCHHPGUSR",
                "value": f"SRCHLANG={lang_code}&IG={self._cvid}&SRCHMKT={locale or 'en-US'}",
                "domain": ".bing.com",
                "path": "/"
            },
            {
                "name": "_EDGE_CD",
                "value": f"m={locale or 'en-US'}&u={lang_code}",
                "domain": ".bing.com",
                "path": "/"
            }
        ]
        context.add_cookies(bing_cookies)

    def search_with_page(self, page, query: str, limit: int,
                         locale: Optional[str] = None) -> tuple[list[SearchResult], EngineStatus]:
        """
        Search using a provided page (context already set up).
        Bing-specific: visits homepage first, waits for JS rendering.
        """
        start_time = time.time()
        results = []
        seen_urls = set()
        max_pages = (limit // self.results_per_page) + 1

        try:
            # Visit Bing homepage first to initialize session/cookies properly
            page.goto("https://www.bing.com/", wait_until="domcontentloaded")
            time.sleep(random.uniform(0.5, 1.0))
            self.handle_consent(page)

            for page_num in range(max_pages):
                if len(results) >= limit:
                    break

                # Build URL and navigate
                search_url = self.build_search_url(query, page_num)
                if locale:
                    search_url = self._add_locale_params(search_url, locale)

                page.goto(search_url, wait_until="domcontentloaded")

                # Handle consent popup on first page
                if page_num == 0:
                    self.handle_consent(page)

                # Wait for search results to render (Bing uses JS)
                try:
                    page.wait_for_selector("li.b_algo", timeout=5000)
                except Exception:
                    pass

                # Small random delay to appear human
                time.sleep(random.uniform(0.3, 0.8))

                # Extract results
                page_results = self.extract_results(page)

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

    def search(self, query: str, limit: int, headless: bool = True,
               locale: Optional[str] = None) -> tuple[list[SearchResult], EngineStatus]:
        """
        Perform Bing search with proper locale/cookie handling.
        Creates its own browser - use search_with_page() for shared browser.
        """
        from camoufox.sync_api import Camoufox

        start_time = time.time()

        # Generate new cvid for this search session
        self._cvid = self._generate_cvid()

        # Parse locale for Camoufox config
        lang_code = "en"
        region_code = "US"
        if locale:
            parts = locale.split("-")
            lang_code = parts[0]
            if len(parts) > 1:
                region_code = parts[1]

        # Build comprehensive Camoufox options for locale spoofing
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
                # Create context with Bing-specific options
                context_opts = self.get_context_options(locale)
                context = browser.new_context(**context_opts)

                # Set up Bing cookies
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
            return [], EngineStatus(
                engine=self.name, success=False,
                result_count=0, elapsed_time=elapsed, error=str(e)
            )

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
