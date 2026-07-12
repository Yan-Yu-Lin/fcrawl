# Google Search Descriptions Investigation

## Problem Statement

The `csearch` command extracts descriptions from Google's rendered DOM using `div.VwiC3b` selector, but the text is truncated with "..." (typically ~160 characters). SearXNG appears to return fuller descriptions.

## Key Findings

### 1. Truncation is Server-Side, Not CSS

**Finding**: Google truncates snippet text on the server before sending HTML. The truncation is NOT caused by CSS `webkit-line-clamp`.

Evidence:
- `div.VwiC3b` content is already truncated with "..." in the HTML
- `div[data-sncf="1"]` (SearXNG's selector) returns identical truncated text
- Removing CSS line-clamp via JavaScript does not reveal more text
- `scrollHeight === clientHeight` confirms no hidden overflow content

```javascript
// Test result: No hidden content
{
  "beforeLen": 162,
  "afterLen": 162,  // Same after removing line-clamp
  "changed": false
}
```

### 2. SearXNG Uses the Same Truncated Data

**Finding**: SearXNG extracts from the same elements and gets the same truncated text.

SearXNG's extraction (from `searx/engines/google.py`):
```python
# Result container
for result in eval_xpath_list(dom, './/div[contains(@jscontroller, "SC7lYd")]'):
    # Content extraction
    content_nodes = eval_xpath(result, './/div[contains(@data-sncf, "1")]')
    content = extract_text(content_nodes)
```

This is functionally equivalent to our `div.VwiC3b` approach.

### 3. Why SearXNG APPEARS to Have Fuller Descriptions

Possible reasons:
1. **Caching/aggregation**: SearXNG may cache results from multiple fetches
2. **Regional variation**: Different Google domains (google.de, google.co.uk) may return slightly different snippets
3. **Featured snippets**: SearXNG may be extracting from featured snippets which have longer text
4. **Query-dependent**: Some queries trigger knowledge panels with fuller descriptions

### 4. Fuller Text EXISTS in Specific Places

**Featured Snippets** (`span.hgKElc`):
- 200-260+ characters
- Only available for certain queries (not all results)
- Contains authoritative summary content

**Knowledge Panels** (`div[data-attrid]`):
- 200-270+ characters
- Only for entities Google has knowledge about
- Contains structured information

Example from testing:
```
Featured snippet (len=262):
"Our newest model, Claude Opus 4.5, is available today. It's intelligent,
efficient, and the best model in the world for coding, agents, and computer
use. It's also meaningfully better at everyday task"
```

vs Standard snippet (len=162):
```
"Nov 24, 2025 — At its highest effort level, Opus 4.5 exceeds Sonnet 4.5
performance by 4.3 percentage points—while using 48% fewer tokens..."
```

## Google DOM Structure

```
div[jscontroller="SC7lYd"]  (result container)
├── div[data-snf="x5WNvb"]  (title/URL section)
│   └── div.yuRUbf
│       └── a[jsname="UWckNb"]
│           └── h3.LC20lb (title)
└── div[data-sncf="1"]      (snippet section)
    └── div.VwiC3b          (snippet text, truncated)
```

## Selectors Reference

| Selector | Purpose | Text Length |
|----------|---------|-------------|
| `div.VwiC3b` | Standard snippet | ~160 chars (truncated) |
| `div[data-sncf="1"]` | Same as above (SearXNG) | ~160 chars (truncated) |
| `span.hgKElc` | Featured snippet | 200-300 chars |
| `div[data-attrid]` | Knowledge panel | 200-300 chars |
| `div[jscontroller="SC7lYd"]` | Result container | - |
| `a[jsname="UWckNb"]` | Result link | - |

## Recommendations

### Option A: Multi-Source Extraction (Recommended)

Extract from multiple sources and prefer longer descriptions:

```python
def extract_results(self, page) -> list[SearchResult]:
    results = []

    # 1. Check for featured snippets first (longer descriptions)
    featured = {}
    for elem in page.locator("span.hgKElc").all():
        text = elem.text_content()
        if text and len(text) > 50:
            # Store by nearby URL or position
            parent = elem.locator("xpath=ancestor::div[.//a[contains(@href, 'http')]]").first
            if parent.count() > 0:
                link = parent.locator("a").first
                url = link.get_attribute("href") if link.count() > 0 else ""
                if url:
                    featured[url] = text.strip()

    # 2. Extract standard results
    containers = page.locator("div[jscontroller='SC7lYd']").all()
    for elem in containers:
        title_elem = elem.locator("h3").first
        title = title_elem.text_content() if title_elem.count() > 0 else ""

        link_elem = elem.locator("a[jsname='UWckNb']").first
        url = link_elem.get_attribute("href") if link_elem.count() > 0 else ""

        # Standard snippet
        desc_elem = elem.locator("div[data-sncf='1']").first
        description = desc_elem.text_content() if desc_elem.count() > 0 else ""

        # Use featured snippet if available and longer
        if url in featured and len(featured[url]) > len(description):
            description = featured[url]

        if url and url.startswith("http"):
            results.append(SearchResult(
                title=title.strip(),
                url=url,
                description=description.strip(),
                engine=self.name,
                position=len(results) + 1
            ))

    return results
```

### Option B: Scrape Meta Descriptions

For maximum description length, scrape actual pages:

```python
async def get_full_description(self, url: str) -> str:
    """Fetch meta description from actual page"""
    page = await self.browser.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=5000)
        meta = await page.evaluate('''() => {
            const desc = document.querySelector('meta[name="description"]');
            const og = document.querySelector('meta[property="og:description"]');
            return (og?.content || desc?.content || '').substring(0, 500);
        }''')
        return meta
    finally:
        await page.close()
```

**Trade-off**: Much slower (extra HTTP requests per result)

### Option C: Hybrid with Caching

Store and reuse longer descriptions when found:

```python
class DescriptionCache:
    """Cache longer descriptions by URL"""
    _cache: dict[str, str] = {}

    @classmethod
    def get(cls, url: str, default: str) -> str:
        cached = cls._cache.get(url, "")
        return cached if len(cached) > len(default) else default

    @classmethod
    def update(cls, url: str, description: str):
        if len(description) > len(cls._cache.get(url, "")):
            cls._cache[url] = description
```

## SearXNG Implementation Details

SearXNG's async mode request:
```python
# URL format
params = {
    'q': query,
    'hl': 'en-US',
    'filter': '0',
    'start': start,
    'asearch': 'arc',
    'async': f'arc_id:srp_{random_id}_1{start:02},use_ac:true,_fmt:prog'
}
```

Response parsing:
```python
# Uses lxml for parsing
dom = html.fromstring(resp.text)
for result in eval_xpath_list(dom, './/div[contains(@jscontroller, "SC7lYd")]'):
    content_nodes = eval_xpath(result, './/div[contains(@data-sncf, "1")]')
    content = extract_text(content_nodes)  # Still truncated
```

## Conclusion

**The fundamental limitation**: Google truncates snippets server-side. There is no way to get fuller standard snippets from the SERP.

**Best approach for fcrawl**:
1. Continue using current extraction for standard results
2. Add featured snippet extraction (`span.hgKElc`) as a supplementary source
3. When featured snippets exist, prefer their longer descriptions
4. Consider adding `--full-descriptions` flag that scrapes actual pages

**The SearXNG mystery**: Their "fuller" descriptions likely come from featured snippets being mixed with standard results, or from caching/aggregation across multiple requests.

---

## TODO: Future Implementation

**Status**: Documented, not yet implemented

**File to modify**: `src/fcrawl/engines/google.py`

### Tasks

- [ ] Add `_extract_featured_snippets()` method to extract from `span.hgKElc`
- [ ] Update `extract_results()` to use multi-source extraction (Option A above)
- [ ] Prefer longer descriptions when featured snippets are available
- [ ] Consider adding `--full-descriptions` flag for meta description scraping (Option B)

### Priority

Low - Current truncated descriptions are functional. This is an enhancement for better UX.

### Related Files

- `src/fcrawl/engines/google.py` - GoogleEngine implementation
- `src/fcrawl/engines/bing.py` - May need similar investigation
- `src/fcrawl/engines/brave.py` - May need similar investigation
