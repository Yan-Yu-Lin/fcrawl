# SearXNG Reliability Issues & Debug Mode Implementation

**Date:** 2026-01-03
**Context:** fcrawl CLI search functionality investigation and improvements

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Problem Overview](#problem-overview)
3. [Root Cause Analysis](#root-cause-analysis)
4. [How Search Engines Detect and Block SearXNG](#how-search-engines-detect-and-block-searxng)
5. [Camoufox (gsearch) as Alternative](#camoufox-gsearch-as-alternative)
6. [Debug Mode Implementation](#debug-mode-implementation)
7. [Code Changes Made](#code-changes-made)
8. [How SearXNG Aggregates Results](#how-searxng-aggregates-results)
9. [Future Solutions](#future-solutions)
10. [Key Files Reference](#key-files-reference)

---

## Architecture Overview

### Complete Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         USER TERMINAL                                        │
│  $ fcrawl search "python" -l 20 --debug --no-cache                          │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  fcrawl CLI (Python)                                                         │
│  ~/firecrawl-workspace/scripts/fcrawl/src/fcrawl/commands/search.py         │
│                                                                              │
│  1. Parse CLI args: query="python", limit=20, debug=True                    │
│  2. Build search_options dict                                                │
│  3. Check cache (skip if --no-cache)                                        │
│  4. Call: client.search(**search_options)                                   │
│                                                                              │
│  client = FirecrawlApp(api_url="http://localhost:3002")                     │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      │ HTTP POST /v2/search
                                      │ Body: {"query": "python", "limit": 20}
                                      │ Header: Authorization: Bearer fc-test
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  firecrawl-py SDK (Python)                                                   │
│  apps/python-sdk/firecrawl/v2/methods/search.py                             │
│                                                                              │
│  1. Validate request (query not empty, limit <= 100)                        │
│  2. POST to API: client.post("/v2/search", request_data)                    │
│  3. Parse response into SearchData model                                    │
│  4. Map fields: unresponsiveEngines → unresponsive_engines                  │
│  5. Return SearchData(web=[...], unresponsive_engines=[...])               │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      │ HTTP POST http://localhost:3002/v2/search
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  Firecrawl API (TypeScript/Node.js) - Docker Container                      │
│  apps/api/src/controllers/v2/search.ts                                      │
│                                                                              │
│  1. Authenticate request (bypassed for localhost)                           │
│  2. Parse body: { query, limit, scrapeOptions, ... }                        │
│  3. Determine search provider:                                              │
│     - If SEARXNG_ENDPOINT set → use SearXNG                                 │
│     - Else → use default (Serper/Google API)                                │
│  4. Call: searxng_search(query, { num_results: limit * 2 })                │
│                                                                              │
│  ENV: SEARXNG_ENDPOINT=http://searxng:8080                                  │
│       SEARXNG_ENGINES=brave,bing,duckduckgo,startpage,google                │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  SearXNG Search Module (TypeScript)                                          │
│  apps/api/src/search/v2/searxng.ts                                          │
│                                                                              │
│  Pagination Loop:                                                            │
│  ┌────────────────────────────────────────────────────────────────────┐     │
│  │  while (results.length < targetCount && page <= maxPages) {        │     │
│  │                                                                     │     │
│  │    GET http://searxng:8080/search                                  │     │
│  │        ?q=python                                                    │     │
│  │        &engines=brave,bing,duckduckgo,startpage,google             │     │
│  │        &pageno=1,2,3...                                            │     │
│  │        &format=json                                                 │     │
│  │                                                                     │     │
│  │    Deduplicate by URL (seenUrls Set)                               │     │
│  │    Capture: engine, engines[], score, unresponsive_engines         │     │
│  │    page++                                                           │     │
│  │  }                                                                  │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│                                                                              │
│  Return: { web: [...], unresponsiveEngines: [...] }                         │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                                      │ HTTP GET http://searxng:8080/search?...
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│  SearXNG Container (Python/Flask)                                            │
│  Docker: localhost:8888 (external) → searxng:8080 (internal)                │
│  Config: searxng/settings.yml                                                │
│                                                                              │
│  ┌─────────────────────────────────────────────────────────────────────┐    │
│  │                    PARALLEL ENGINE QUERIES                          │    │
│  │                                                                      │    │
│  │   ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  │    │
│  │   │  Brave  │  │  Bing   │  │ DuckDuck│  │Startpage│  │ Google  │  │    │
│  │   │         │  │         │  │   Go    │  │         │  │         │  │    │
│  │   └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘  └────┬────┘  │    │
│  │        │            │            │            │            │        │    │
│  │        ▼            ▼            ▼            ▼            ▼        │    │
│  │     20 results   10 results   CAPTCHA     CAPTCHA     0 results    │    │
│  │        ✓            ✓           ✗           ✗         (blocked)    │    │
│  │                                                                      │    │
│  └─────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  Aggregation:                                                                │
│  1. Collect results from all responding engines                             │
│  2. Deduplicate by URL (merge if same URL from multiple engines)           │
│  3. Score results:                                                           │
│     - Higher position in engine = higher score                              │
│     - Found by multiple engines = higher score                              │
│  4. Sort by score descending                                                │
│  5. Track unresponsive_engines: [["duckduckgo","CAPTCHA"], ...]            │
│                                                                              │
│  Return JSON:                                                                │
│  {                                                                           │
│    "results": [                                                              │
│      {"url": "...", "title": "...", "engine": "brave", "engines": ["brave"]}│
│    ],                                                                        │
│    "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ...]                 │
│  }                                                                           │
└─────────────────────────────────────┬───────────────────────────────────────┘
                                      │
                      ┌───────────────┴───────────────┐
                      │     EXTERNAL SEARCH ENGINES   │
                      │         (The Internet)        │
                      └───────────────────────────────┘
                                      │
        ┌─────────────┬───────────────┼───────────────┬─────────────┐
        ▼             ▼               ▼               ▼             ▼
   ┌─────────┐  ┌─────────┐    ┌───────────┐   ┌──────────┐  ┌─────────┐
   │  Brave  │  │  Bing   │    │ DuckDuckGo│   │Startpage │  │ Google  │
   │ Search  │  │ Search  │    │  Search   │   │  Search  │  │ Search  │
   └─────────┘  └─────────┘    └───────────┘   └──────────┘  └─────────┘
        │             │               │               │             │
        ▼             ▼               ▼               ▼             ▼
     ✓ OK          ✓ OK          ✗ CAPTCHA      ✗ CAPTCHA      ✗ Blocked
   (20 results)  (10 results)    (detected)    (suspended)   (0 results)
```

### Response Flow (Reverse Path)

```
SearXNG → Firecrawl API:
{
  "results": [
    {"url": "python.org", "title": "Welcome", "engine": "brave", "engines": ["brave"], "score": 1.0},
    {"url": "stackoverflow.com/...", "engine": "bing", "engines": ["bing"], "score": 0.9},
    ...
  ],
  "unresponsive_engines": [["duckduckgo", "CAPTCHA"], ["startpage", "Suspended: CAPTCHA"]]
}

Firecrawl API → SDK:
{
  "success": true,
  "data": {
    "web": [
      {"url": "python.org", "title": "Welcome", "engine": "brave", "engines": ["brave"]},
      ...
    ],
    "unresponsiveEngines": [["duckduckgo", "CAPTCHA"], ...]
  }
}

SDK → fcrawl CLI:
SearchData(
  web=[SearchResultWeb(url="python.org", engine="brave", engines=["brave"]), ...],
  unresponsive_engines=[["duckduckgo", "CAPTCHA"], ...]
)

fcrawl CLI → Terminal:
═══ DEBUG: Engine Status ═══
  ✗ duckduckgo: CAPTCHA
  ✗ startpage: Suspended: CAPTCHA
Results by engine:
  ✓ brave: 11 results
  ✓ bing: 9 results

(brave) ## Welcome to Python.org
https://python.org
...
```

### Docker Network Topology

```
┌─────────────────────────────────────────────────────────┐
│  Docker Network: firecrawl_default                      │
│                                                          │
│  ┌──────────────┐    ┌──────────────┐    ┌───────────┐ │
│  │ firecrawl-   │    │  firecrawl-  │    │ firecrawl-│ │
│  │ api-1        │───▶│  searxng     │    │ redis-1   │ │
│  │ :3002        │    │  :8080       │    │ :6379     │ │
│  └──────────────┘    └──────────────┘    └───────────┘ │
│         ▲                   ▲                           │
└─────────┼───────────────────┼───────────────────────────┘
          │                   │
     localhost:3002      localhost:8888
          │                   │
          └───────────────────┘
                   │
            ┌──────┴──────┐
            │   fcrawl    │
            │   (host)    │
            └─────────────┘
```

### Key Files by Layer

| Layer | File | Purpose |
|-------|------|---------|
| CLI | `fcrawl/commands/search.py` | Parse args, display results |
| SDK | `firecrawl/v2/methods/search.py` | HTTP client, type mapping |
| SDK | `firecrawl/v2/types.py` | Pydantic models |
| API | `api/src/controllers/v2/search.ts` | Route handler |
| API | `api/src/search/v2/searxng.ts` | SearXNG integration |
| API | `api/src/lib/entities.ts` | TypeScript interfaces |
| Config | `.env` | `SEARXNG_ENGINES=brave,bing,...` |
| Config | `searxng/settings.yml` | Engine definitions |

---

## Problem Overview

### Symptom
`fcrawl search "query" -l 30` was only returning ~10 results (sometimes only 1), regardless of the limit specified.

### Initial Suspicion
We thought it was a pagination bug in Firecrawl - that the API wasn't fetching multiple pages from SearXNG.

### Actual Root Cause
**The Firecrawl pagination code is correct.** The real issue is that **SearXNG's upstream search engines are being blocked/rate-limited**.

When we tested SearXNG directly:
```bash
curl "http://localhost:8888/search?q=python&format=json"
```

Response showed:
```json
{
  "results": [{"engine": "wikipedia", ...}],  // Only 1 result!
  "unresponsive_engines": [
    ["brave", "Suspended: too many requests"],
    ["duckduckgo", "CAPTCHA"],
    ["startpage", "Suspended: CAPTCHA"],
    ["google", ""]  // Returns 0 results
  ]
}
```

### Quick Fix
Restarting SearXNG clears the rate limit state:
```bash
docker compose restart searxng
```

After restart, search returns 25-30 results with only some engines blocked.

---

## Root Cause Analysis

### The Data Flow

```
fcrawl search command
    ↓
Firecrawl Python SDK (firecrawl-py)
    ↓ POST /v2/search
Firecrawl API (apps/api/src/controllers/v2/search.ts)
    ↓ calls search()
Search Module (apps/api/src/search/v2/index.ts)
    ↓ if SEARXNG_ENDPOINT set
SearXNG Module (apps/api/src/search/v2/searxng.ts)
    ↓ HTTP GET with pageno parameter
SearXNG Container (localhost:8888)
    ↓ queries multiple engines
Google, Bing, DuckDuckGo, Brave, etc.
    ↓ BLOCKED/RATE-LIMITED
Returns minimal or no results
```

### Pagination Logic (Already Correct)

The `searxng.ts` file already has proper pagination:

```typescript
// apps/api/src/search/v2/searxng.ts
const maxPages = Math.ceil(targetCount / 10) + 1;

while (allResults.length < targetCount && page <= maxPages) {
  const params = {
    q: q,
    pageno: page,  // SearXNG pagination parameter
    format: "json",
  };
  // ... fetch and deduplicate
  page++;
}
```

The problem isn't pagination - it's that SearXNG engines return empty/blocked responses.

---

## How Search Engines Detect and Block SearXNG

### Detection Vectors

| Vector | SearXNG Behavior | Why It's Detected |
|--------|-----------------|-------------------|
| **IP Reputation** | Single container IP | Too many requests from same IP |
| **User-Agent** | Generic/bot-like | No real browser signature |
| **JavaScript** | None executed | Bots don't run JS |
| **Cookies** | None/fresh each request | No session continuity |
| **Fingerprint** | None | Missing canvas, WebGL, fonts |
| **Request Pattern** | Fast, sequential | pageno=1,2,3 is obvious scraping |
| **Behavior** | No mouse/scroll | Missing human interaction signals |

### What SearXNG Sends to Search Engines

SearXNG makes raw HTTP requests using Python's `requests` library:

```
GET /search?q=python HTTP/1.1
Host: www.google.com
User-Agent: Mozilla/5.0 (compatible; Searx)
Accept: text/html
Accept-Language: en-US,en
Cookie: (none)
Referer: (none)

Missing:
- No JavaScript execution
- No browser fingerprint
- No mouse movements
- No session cookies
- No realistic timing
```

### reCAPTCHA v3 Scoring

Google uses reCAPTCHA v3 which assigns trust scores (0.0 to 1.0):
- **1.0** = Definitely human
- **0.0** = Definitely bot

Score factors:
- Behavioral analysis (mouse, typing)
- Session history
- Device fingerprinting
- IP reputation
- Cookie consistency

SearXNG gets low scores because it has none of these signals.

---

## Camoufox (gsearch) as Alternative

### What is Camoufox?

Camoufox is an anti-detection Firefox browser wrapper built on Playwright. It:
- Runs a real Firefox browser
- Spoofs fingerprints to appear consistent
- Simulates human-like behavior
- Maintains session cookies

### gsearch Command

We have a working `gsearch` command that uses Camoufox:

```bash
fcrawl gsearch "python tutorials" -l 20
fcrawl gsearch "AI news" --locale ja-JP
fcrawl gsearch "query" --headful  # Debug mode with visible browser
```

### How gsearch Works

```python
# fcrawl/commands/gsearch.py

from camoufox.sync_api import Camoufox

camoufox_opts = {
    "headless": True,          # Run without visible window
    "humanize": True,          # Simulate human-like behavior
    "block_images": True,      # Faster loading
    "os": _get_os_name(),      # Match fingerprint to your OS
    "locale": "en-US",         # Set browser locale
}

with Camoufox(**camoufox_opts) as browser:
    page = browser.new_page()
    page.goto(f"https://www.google.com/search?q={query}")
    # Extract results from real rendered page
```

### Camoufox vs SearXNG Comparison

| Feature | SearXNG | Camoufox (gsearch) |
|---------|---------|-------------------|
| Request Type | Raw HTTP | Real browser |
| JavaScript | None | Full execution |
| Fingerprint | None | Spoofed but consistent |
| Cookies | None | Session maintained |
| Human behavior | None | humanize=True |
| Detection resistance | Low | High |
| Speed | Fast | Slower (~2-5s per search) |
| Resource usage | Low | High (runs Firefox) |

### gsearch Features Implemented

1. **Pagination**: Fetches multiple pages using `&start=` parameter
2. **Locale support**: `--locale ja-JP` for regional results
3. **Description extraction**: Parses Google's DOM for snippets
4. **Cookie consent handling**: Auto-accepts popups
5. **Cache support**: Results cached locally

---

## Debug Mode Implementation

### What We Built

Added `--debug` flag to `fcrawl search` that shows:
1. Which engines are responding/blocked
2. How many results came from each engine
3. Engine source tag for each result

### Usage

```bash
fcrawl search "python" --debug
```

### Output Example

```
✓ Found 10 results

═══ DEBUG: Engine Status ═══
Unresponsive engines:
  ✗ startpage: Suspended: CAPTCHA

Results by engine:
  ✓ brave: 5 results
  ✓ duckduckgo: 5 results
════════════════════════════

                     🌐 Web Results
────────────────────────────────────────────────────────────
(duckduckgo, brave) ## Python Tutorial - W3Schools
https://www.w3schools.com/python/
...

(brave) ## Welcome to Python.org
https://www.python.org/
...
```

---

## Code Changes Made

### 1. Firecrawl API - Type Definitions

**File:** `apps/api/src/lib/entities.ts`

Added engine metadata fields to `WebSearchResult`:
```typescript
export interface WebSearchResult {
  url: string;
  title: string;
  description: string;
  position?: number;
  category?: string;
  // NEW: Engine metadata (from SearXNG)
  engine?: string;           // Primary engine that found this result
  engines?: string[];        // All engines that found this result
  score?: number;            // SearXNG ranking score
  // ... existing fields
}
```

Added `unresponsiveEngines` to `SearchV2Response`:
```typescript
export interface SearchV2Response {
  web?: WebSearchResult[];
  images?: ImageSearchResult[];
  news?: NewsSearchResult[];
  // NEW: Engine status metadata
  unresponsiveEngines?: Array<[string, string]>;  // [[engine, reason], ...]
}
```

### 2. Firecrawl API - SearXNG Search

**File:** `apps/api/src/search/v2/searxng.ts`

Modified to pass through engine metadata:
```typescript
// Capture unresponsive engines from first page
if (page === 1 && data.unresponsive_engines) {
  unresponsiveEngines = data.unresponsive_engines;
}

// Include engine metadata in each result
allResults.push({
  url: a.url,
  title: a.title,
  description: a.content,
  // NEW: Include engine metadata
  engine: a.engine,
  engines: a.engines,
  score: a.score,
});

// Return with unresponsiveEngines
return {
  web: allResults,
  unresponsiveEngines,
};
```

### 3. Python SDK - Type Definitions

**File:** `apps/python-sdk/firecrawl/v2/types.py`

Added fields to `SearchResultWeb`:
```python
class SearchResultWeb(BaseModel):
    url: str
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    # NEW: Engine metadata (from SearXNG)
    engine: Optional[str] = None
    engines: Optional[List[str]] = None
    score: Optional[float] = None
```

Added to `SearchData`:
```python
class SearchData(BaseModel):
    web: Optional[List[Union[SearchResultWeb, Document]]] = None
    news: Optional[List[Union[SearchResultNews, Document]]] = None
    images: Optional[List[Union[SearchResultImages, Document]]] = None
    # NEW: Engine status metadata
    unresponsive_engines: Optional[List[List[str]]] = None
```

### 4. Python SDK - Search Method

**File:** `apps/python-sdk/firecrawl/v2/methods/search.py`

Added handling for `unresponsiveEngines`:
```python
# Capture engine status metadata (API uses camelCase)
if "unresponsiveEngines" in data:
    out.unresponsive_engines = data["unresponsiveEngines"]
```

### 5. fcrawl CLI - Search Command

**File:** `fcrawl/src/fcrawl/commands/search.py`

Added `--debug` flag and display functions:

```python
@click.option('--debug', is_flag=True, help='Show engine status and result sources')
def search(..., debug: bool):
    # ...
    if debug:
        _display_debug_info(result)

    if pretty and not output and not json_output:
        _display_search_results(result, scrape, debug)


def _display_debug_info(result):
    """Display engine status and statistics"""
    from collections import Counter

    console.print("\n[bold yellow]═══ DEBUG: Engine Status ═══[/bold yellow]")

    # Show unresponsive engines
    unresponsive = getattr(result, 'unresponsive_engines', None)
    if unresponsive:
        console.print("[red]Unresponsive engines:[/red]")
        for engine_info in unresponsive:
            if isinstance(engine_info, (list, tuple)) and len(engine_info) >= 2:
                engine, reason = engine_info[0], engine_info[1]
                console.print(f"  [red]✗[/red] {engine}: {reason}")
    else:
        console.print("[green]All engines responding[/green]")

    # Count results by engine
    engine_counts = Counter()
    if hasattr(result, 'web') and result.web:
        for item in result.web:
            engine = getattr(item, 'engine', 'unknown')
            if engine:
                engine_counts[engine] += 1

    if engine_counts:
        console.print("\n[green]Results by engine:[/green]")
        for engine, count in engine_counts.most_common():
            console.print(f"  [green]✓[/green] {engine}: {count} results")

    console.print("[yellow]════════════════════════════[/yellow]\n")
```

### 6. Rebuild Required

After TypeScript changes, rebuild the API container:
```bash
cd /path/to/firecrawl
docker compose build api
docker compose up -d api
```

After Python SDK changes, reinstall in fcrawl:
```bash
cd fcrawl
uv pip install -e /path/to/firecrawl/apps/python-sdk
```

---

## How SearXNG Aggregates Results

### The Aggregation Process

When you request `limit=10`:

```
SearXNG queries all engines simultaneously:
  Google      → 10 results
  Bing        → 10 results
  DuckDuckGo  → 10 results
  Wikipedia   → 5 results
  ─────────────────────────
  Total: ~35 raw results

SearXNG then:
  1. Deduplicates by URL (same URL from multiple engines merged)
  2. Scores each result:
     - Found by more engines = higher score
     - Higher position in engine results = higher score
  3. Sorts by score
  4. Returns top 10 (your limit)

So ~25 results are "discarded"
```

### Why This is Good

Results found by multiple engines rank higher. The `engines` field shows this:
```json
{"url": "python.org", "engines": ["google", "bing", "duckduckgo"], "score": 3.0}
```

This result was found by 3 engines, so it's likely more relevant.

### SearXNG Configuration

**File:** `searxng/settings.yml`

```yaml
use_default_settings: true

server:
  limiter: false  # Disable internal rate limiting

engines:
  - name: google
    engine: google
    shortcut: g

  - name: bing
    engine: bing
    shortcut: b

  - name: duckduckgo
    engine: duckduckgo
    shortcut: dd

  - name: github
    engine: github
    shortcut: gh
```

---

## Future Solutions

### Option 1: gsearch Fallback

Modify `fcrawl search` to automatically fall back to `gsearch` when SearXNG returns poor results:

```python
result = client.search(**search_options)
if len(result.web or []) < limit / 2:
    console.print("[yellow]SearXNG returned few results, using gsearch...[/yellow]")
    # Fall back to gsearch
```

### Option 2: Multi-Engine Camoufox Search (csearch)

Build a generalized Camoufox search that works with multiple engines:

```bash
fcrawl csearch "python" --engine google
fcrawl csearch "python" --engine bing
fcrawl csearch "python" --engine all  # Query all engines
```

Each engine needs:
- Different URL pattern (`bing.com/search?q=` vs `google.com/search?q=`)
- Different result selectors (different HTML structure)
- Different pagination (`&first=10` vs `&start=10`)

### Option 3: Add More SearXNG Engines

Edit `searxng/settings.yml` to enable more engines for resilience:
- Qwant
- Mojeek
- Yahoo
- Yandex (requires proxy)

More engines = more chances of getting results when some are blocked.

### Option 4: Proxy Rotation for SearXNG

Configure SearXNG to use rotating proxies to avoid IP-based blocking. This requires:
- Proxy service subscription
- SearXNG proxy configuration
- Per-engine proxy settings

---

## Key Files Reference

### Firecrawl API
- `apps/api/src/lib/entities.ts` - Type definitions
- `apps/api/src/search/v2/searxng.ts` - SearXNG integration
- `apps/api/src/search/v2/index.ts` - Search router
- `apps/api/src/controllers/v2/search.ts` - Search controller

### Python SDK
- `apps/python-sdk/firecrawl/v2/types.py` - Type definitions
- `apps/python-sdk/firecrawl/v2/methods/search.py` - Search method

### fcrawl CLI
- `fcrawl/src/fcrawl/commands/search.py` - Search command with debug
- `fcrawl/src/fcrawl/commands/gsearch.py` - Camoufox Google search

### Configuration
- `searxng/settings.yml` - SearXNG engine configuration
- `docker-compose.override.yaml` - SearXNG container setup

---

## Testing Commands

```bash
# Test SearXNG directly
curl "http://localhost:8888/search?q=python&format=json" | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('Results:', len(d.get('results',[])))
print('Unresponsive:', d.get('unresponsive_engines',[]))
"

# Test Firecrawl API directly
curl -X POST http://localhost:3002/v2/search \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer fc-test" \
  -d '{"query": "python", "limit": 5}' | python3 -m json.tool

# Test fcrawl search with debug
fcrawl search "python" -l 10 --debug --no-cache

# Test gsearch (Camoufox)
fcrawl gsearch "python" -l 20

# Restart SearXNG to clear rate limits
docker compose restart searxng
```

---

## Summary

1. **SearXNG reliability issues** are caused by upstream search engines blocking/rate-limiting requests
2. **The Firecrawl pagination code is correct** - it already loops through pages
3. **gsearch (Camoufox)** is a working alternative that bypasses detection
4. **Debug mode** (`--debug`) now shows engine status and result sources
5. **Quick fix**: `docker compose restart searxng` clears rate limits
6. **Long-term**: Consider gsearch fallback or multi-engine Camoufox
