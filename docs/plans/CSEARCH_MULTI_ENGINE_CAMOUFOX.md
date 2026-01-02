# Plan: csearch - Multi-Engine Camoufox Search

**Date:** 2026-01-03
**Status:** Planned
**Author:** Claude Code

---

## Vision

Replace the unreliable SearXNG-based search with a Camoufox-powered multi-engine search aggregator that:
- Uses a real browser to avoid detection/blocking
- Queries multiple search engines in parallel
- Aggregates and ranks results like SearXNG does
- Provides the same `--debug` visibility into engine status

---

## Problem Statement

### Current Architecture: SearXNG

```
fcrawl search → Firecrawl API → SearXNG → [Google, Bing, DDG, Brave, ...]
                                              ↓
                                    Raw HTTP requests
                                              ↓
                                    Easily detected → BLOCKED
```

**Issues:**
- Search engines detect and block SearXNG's raw HTTP requests
- DuckDuckGo returns CAPTCHA
- Startpage returns CAPTCHA
- Google returns 0 results (silently blocked)
- Only Brave and Bing work reliably
- Restarting SearXNG only temporarily fixes rate limits

### Why Engines Block SearXNG

| Detection Vector | SearXNG Behavior | Why Detected |
|------------------|------------------|--------------|
| IP Reputation | Single container IP | Too many requests |
| User-Agent | Generic/bot-like | No real browser signature |
| JavaScript | None executed | Bots don't run JS |
| Cookies | None/fresh each request | No session continuity |
| Fingerprint | None | Missing canvas, WebGL, fonts |
| Request Pattern | Fast, sequential | `pageno=1,2,3` is obvious |
| Behavior | No mouse/scroll | Missing human signals |

---

## Solution: Camoufox Multi-Engine Search

### What is Camoufox?

Camoufox is an anti-detection Firefox browser wrapper built on Playwright:
- Runs a **real Firefox browser**
- **Spoofs fingerprints** to appear consistent
- **Simulates human behavior** (mouse, typing, scrolling)
- **Maintains session cookies**
- Appears as a real user to search engines

### Proposed Architecture

```
fcrawl csearch "query" --engines google,bing,ddg
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│  csearch command (fcrawl/commands/csearch.py)           │
│                                                          │
│  1. Launch Camoufox browser                             │
│  2. Open tabs for each engine (parallel)                │
│  3. Navigate to search URLs                             │
│  4. Parse results using engine-specific selectors       │
│  5. Aggregate, deduplicate, score                       │
│  6. Return ranked results                               │
└─────────────────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│  Camoufox Browser (anti-detection Firefox)              │
│                                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐              │
│  │  Tab 1   │  │  Tab 2   │  │  Tab 3   │              │
│  │  Google  │  │   Bing   │  │   DDG    │              │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘              │
│       │             │             │                     │
│       ▼             ▼             ▼                     │
│  Real browser requests (appear human)                   │
└─────────────────────────────────────────────────────────┘
         │
         ▼
   Search Engines (not blocked)
```

---

## Implementation Plan

### Phase 1: Core csearch Command

**File:** `fcrawl/src/fcrawl/commands/csearch.py`

```python
@click.command()
@click.argument('query')
@click.option('--engines', '-e', default='google,bing,brave',
              help='Comma-separated list of engines')
@click.option('--limit', '-l', type=int, default=20)
@click.option('--debug', is_flag=True)
@click.option('--headful', is_flag=True, help='Show browser window')
def csearch(query, engines, limit, debug, headful):
    """Multi-engine search using Camoufox browser"""
    pass
```

### Phase 2: Engine Definitions

**File:** `fcrawl/src/fcrawl/engines/` (new module)

```
engines/
├── __init__.py
├── base.py          # Abstract base class
├── google.py        # Google-specific selectors
├── bing.py          # Bing-specific selectors
├── duckduckgo.py    # DDG-specific selectors
├── brave.py         # Brave-specific selectors
└── aggregator.py    # Result merging logic
```

### Phase 3: Engine Selector Definitions

| Engine | URL Pattern | Pagination |
|--------|-------------|------------|
| Google | `google.com/search?q={query}` | `&start=10,20,30` |
| Bing | `bing.com/search?q={query}` | `&first=11,21,31` |
| DuckDuckGo | `duckduckgo.com/?q={query}` | Infinite scroll / `&s=30` |
| Brave | `search.brave.com/search?q={query}` | `&offset=10,20` |

**Google Selectors:**
```python
GOOGLE = {
    "url": "https://www.google.com/search?q={query}",
    "selectors": {
        "results": "div.g",
        "title": "h3",
        "url": "a[href]",
        "description": "div[data-sncf], div.VwiC3b, span.aCOpRe"
    },
    "pagination": {
        "type": "url_param",
        "param": "start",
        "increment": 10
    },
    "consent_handler": {
        "selector": "button[id*='accept'], form[action*='consent'] button",
        "action": "click"
    }
}
```

**Bing Selectors:**
```python
BING = {
    "url": "https://www.bing.com/search?q={query}",
    "selectors": {
        "results": "li.b_algo",
        "title": "h2 a",
        "url": "h2 a",
        "description": "p.b_lineclamp2, p.b_algoSlug"
    },
    "pagination": {
        "type": "url_param",
        "param": "first",
        "increment": 10,
        "start": 11  # Bing starts at 11, not 10
    }
}
```

**DuckDuckGo Selectors:**
```python
DUCKDUCKGO = {
    "url": "https://duckduckgo.com/?q={query}",
    "selectors": {
        "results": "article[data-testid='result']",
        "title": "h2 a span",
        "url": "a[data-testid='result-title-a']",
        "description": "span[class*='snippet']"
    },
    "pagination": {
        "type": "scroll",  # DDG uses infinite scroll
        "wait_selector": "article[data-testid='result']"
    }
}
```

**Brave Selectors:**
```python
BRAVE = {
    "url": "https://search.brave.com/search?q={query}",
    "selectors": {
        "results": "div.snippet",
        "title": "a.result-header span.title",
        "url": "a.result-header",
        "description": "p.snippet-description"
    },
    "pagination": {
        "type": "url_param",
        "param": "offset",
        "increment": 10
    }
}
```

### Phase 4: Aggregation Logic

```python
def aggregate_results(all_results: list) -> list:
    """
    Merge results from multiple engines.

    Scoring:
    - Found by N engines = score N
    - Higher position in original results = bonus
    - Deduplicate by normalized URL
    """
    by_url = {}

    for result in all_results:
        url = normalize_url(result["url"])

        if url in by_url:
            # Same URL found by another engine
            by_url[url]["engines"].append(result["engine"])
            by_url[url]["score"] += 1
        else:
            by_url[url] = {
                "url": result["url"],
                "title": result["title"],
                "description": result["description"],
                "engine": result["engine"],      # Primary engine
                "engines": [result["engine"]],   # All engines
                "score": 1,
                "position": result["position"]   # Original rank
            }

    # Sort by: score DESC, then position ASC
    ranked = sorted(
        by_url.values(),
        key=lambda x: (-x["score"], x["position"])
    )

    return ranked
```

### Phase 5: Parallel Execution

```python
async def search_parallel(query: str, engines: list, limit: int):
    """Query multiple engines in parallel using browser tabs"""

    async with AsyncCamoufox(headless=True, humanize=True) as browser:
        context = await browser.new_context()

        # Create tasks for each engine
        tasks = []
        for engine_name in engines:
            task = search_engine(context, query, engine_name, limit)
            tasks.append(task)

        # Run all in parallel
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Flatten and aggregate
        all_results = []
        for engine_name, engine_results in zip(engines, results):
            if isinstance(engine_results, Exception):
                console.print(f"[red]✗ {engine_name}: {engine_results}[/red]")
            else:
                all_results.extend(engine_results)

        return aggregate_results(all_results)
```

---

## CLI Interface

### Basic Usage

```bash
# Default: Google, Bing, Brave
fcrawl csearch "python tutorials"

# Specific engines
fcrawl csearch "python" --engines google,bing

# All supported engines
fcrawl csearch "python" --engines google,bing,ddg,brave

# With debug info
fcrawl csearch "python" --debug

# Show browser (debugging)
fcrawl csearch "python" --headful

# With limit
fcrawl csearch "python" -l 30 --engines google,bing
```

### Debug Output

```
═══ csearch: Multi-Engine Browser Search ═══

Querying engines: google, bing, ddg, brave

Engine Status:
  ✓ google: 10 results (1.2s)
  ✓ bing: 10 results (0.9s)
  ✓ ddg: 10 results (1.5s)
  ✓ brave: 10 results (0.8s)

Aggregation:
  Total raw results: 40
  After deduplication: 28

Results by source:
  Found by 4 engines: 3 results
  Found by 3 engines: 5 results
  Found by 2 engines: 8 results
  Found by 1 engine: 12 results

════════════════════════════════════════════

(google, bing, ddg, brave) ## Welcome to Python.org
https://www.python.org/
The official home of the Python Programming Language

(google, bing, brave) ## Python Tutorial - W3Schools
https://www.w3schools.com/python/
Learn Python programming with examples...
```

---

## Comparison: SearXNG vs csearch

| Feature | SearXNG | csearch (Camoufox) |
|---------|---------|-------------------|
| **Detection resistance** | Low (raw HTTP) | High (real browser) |
| **Speed** | Fast (~100ms) | Slower (~2-5s total) |
| **Resource usage** | Low | High (Firefox process) |
| **Blocking** | Frequent CAPTCHAs | Rare |
| **Maintenance** | External project | We control selectors |
| **Parallel queries** | Yes (async HTTP) | Yes (browser tabs) |
| **Customization** | Limited | Full control |
| **Dependencies** | Docker container | Camoufox Python package |

---

## Integration Options

### Option A: Standalone Command (Recommended for Phase 1)

```bash
fcrawl csearch "query"   # Uses Camoufox directly
fcrawl search "query"    # Uses SearXNG via Firecrawl API (existing)
fcrawl gsearch "query"   # Google-only Camoufox (existing)
```

### Option B: Fallback in fcrawl search

```python
# In fcrawl/commands/search.py
result = client.search(**search_options)

if len(result.web or []) < limit / 2:
    console.print("[yellow]SearXNG returned few results, falling back to csearch...[/yellow]")
    result = csearch_fallback(query, limit)
```

### Option C: Replace SearXNG in Firecrawl API

More complex - would require:
1. Running Camoufox as a service
2. Modifying Firecrawl API to call it
3. Handling browser lifecycle in Docker

---

## File Structure

```
fcrawl/src/fcrawl/
├── commands/
│   ├── search.py       # Existing SearXNG-based
│   ├── gsearch.py      # Existing Google-only Camoufox
│   └── csearch.py      # NEW: Multi-engine Camoufox
├── engines/            # NEW: Engine definitions
│   ├── __init__.py
│   ├── base.py
│   ├── google.py
│   ├── bing.py
│   ├── duckduckgo.py
│   ├── brave.py
│   └── aggregator.py
└── utils/
    └── browser.py      # NEW: Shared Camoufox utilities
```

---

## Development Phases

### Phase 1: MVP (1-2 days)
- [ ] Basic csearch command with Google + Bing
- [ ] Sequential engine queries (not parallel yet)
- [ ] Simple aggregation (dedupe by URL)
- [ ] Basic CLI output

### Phase 2: Full Engine Support (1 day)
- [ ] Add DuckDuckGo support
- [ ] Add Brave support
- [ ] Handle consent popups for each engine
- [ ] Pagination for each engine

### Phase 3: Parallel & Performance (1 day)
- [ ] Parallel tab queries
- [ ] Async implementation
- [ ] Caching layer
- [ ] Timeout handling

### Phase 4: Integration (1 day)
- [ ] Debug mode with engine stats
- [ ] Fallback integration with `fcrawl search`
- [ ] Documentation update

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Selectors break when sites update | Monitor and maintain selector configs |
| Slower than SearXNG | Parallel tabs, caching, accept tradeoff for reliability |
| Browser resource usage | Reuse browser instance, close tabs promptly |
| Rate limiting | Humanize delays, rotate user agents |

---

## Success Criteria

1. `fcrawl csearch "python" -l 20` returns 20 results consistently
2. Results come from multiple engines (visible in debug mode)
3. No CAPTCHA or blocking issues
4. Response time < 10 seconds for 20 results
5. Works without Docker (pure Python)

---

## References

- Existing gsearch implementation: `fcrawl/commands/gsearch.py`
- Camoufox docs: https://camoufox.com/
- SearXNG reliability doc: `fcrawl/docs/SEARXNG_RELIABILITY_AND_DEBUG.md`
