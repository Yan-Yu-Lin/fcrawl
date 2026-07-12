# fcrawl Handover - 2026-01-01 Session 2

## Session Summary

This session added `--no-links` option and started implementing caching.

---

## Completed This Session

### 1. `--no-links` Option
**Commit**: `7c74206`

Strips markdown links from output while preserving display text:
- `[text](url)` → `text`
- `![alt](url)` → `[Image: alt]`

**Files modified**:
- `src/fcrawl/utils/output.py` - added `strip_links()` function
- `src/fcrawl/commands/scrape.py` - added `--no-links` flag
- `src/fcrawl/commands/search.py` - added `--no-links` flag
- `src/fcrawl/commands/crawl.py` - added `--no-links` flag

Works with `--article` mode.

---

## In Progress: Caching Feature

### What's Done
1. Created `src/fcrawl/utils/cache.py` with:
   - `cache_key()` - generate hash from URL + options
   - `read_cache()` / `write_cache()` - read/write JSON files
   - `clear_cache()` - delete cache files
   - `result_to_dict()` - convert Firecrawl result to dict
   - `CachedResult` / `CachedMetadata` - wrapper classes

2. Updated `src/fcrawl/commands/scrape.py`:
   - Added `--no-cache` and `--cache-only` flags
   - Added caching logic (check cache → fetch if miss → write to cache)

### What's Left
1. **search.py** - Started adding import, need to:
   - Add `--no-cache` and `--cache-only` options
   - Add caching logic similar to scrape.py
   - Cache key should include: query, sources, category, limit, tbs, location, scrape, formats

2. **crawl.py** - Not started:
   - Add `--no-cache` and `--cache-only` options
   - Cache key: url, limit, depth, include_paths, exclude_paths, formats
   - Cache all crawled pages as single JSON

3. **Test all caching** - Verify it works

---

## Cache Design

**Location**: `/tmp/fcrawl-cache/`
**TTL**: None (let OS clean /tmp)

```
/tmp/fcrawl-cache/
├── scrape/{hash}.json
├── search/{hash}.json
└── crawl/{hash}.json
```

**Flags**:
| Flag | Read Cache | Write Cache | API Call |
|------|------------|-------------|----------|
| (default) | Yes | Yes | If miss |
| `--no-cache` | No | Yes | Always |
| `--cache-only` | Yes | No | Never |

---

## Key Files to Read

1. `src/fcrawl/utils/cache.py` - NEW, cache utilities
2. `src/fcrawl/commands/scrape.py` - Updated with caching
3. `src/fcrawl/commands/search.py` - Partially updated (import added)
4. `src/fcrawl/commands/crawl.py` - Needs caching added
5. `~/.claude/plans/snug-discovering-toast.md` - Full implementation plan

---

## Git Status

```
Modified (not committed):
- src/fcrawl/utils/cache.py (NEW)
- src/fcrawl/commands/scrape.py
- src/fcrawl/commands/search.py (partial)
```

---

## To Continue

1. Read the plan file: `~/.claude/plans/snug-discovering-toast.md`
2. Read `cache.py` to understand the utilities
3. Read updated `scrape.py` to see the caching pattern
4. Apply same pattern to `search.py` and `crawl.py`
5. Test with:
   ```bash
   fcrawl scrape https://example.com  # First time: fetches
   fcrawl scrape https://example.com  # Second time: "Using cached result"
   fcrawl scrape https://example.com --no-cache  # Force fresh
   fcrawl scrape https://example.com --cache-only  # Fail if not cached
   ```
6. Commit and reinstall: `uv tool install . --reinstall`
