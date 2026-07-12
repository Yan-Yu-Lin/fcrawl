# fcrawl Search Architecture Overview

**Date:** 2026-01-03

---

## Current Search Commands

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         fcrawl SEARCH COMMANDS                               │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  fcrawl search     ──→ Firecrawl API ──→ SearXNG ──→ Multiple Engines       │
│  (existing)             (Docker)          (Docker)    (HTTP requests)        │
│                                                        ⚠️ Often blocked      │
│                                                                              │
│  fcrawl gsearch    ──→ Camoufox Browser ──→ Google Only                     │
│  (existing)            (local Python)       ✓ Reliable                       │
│                                                                              │
│  fcrawl csearch    ──→ Camoufox Browser ──→ Multiple Engines                │
│  (planned)             (local Python)       ✓ Reliable                       │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Architecture Comparison

### 1. fcrawl search (SearXNG-based)

```
User Terminal
     │
     ▼
fcrawl CLI ──HTTP──→ Firecrawl API ──HTTP──→ SearXNG Container
                     localhost:3002          localhost:8888
                                                   │
                                    ┌──────────────┼──────────────┐
                                    ▼              ▼              ▼
                                 Google         Bing          Brave ...
                                    │              │              │
                                    ▼              ▼              ▼
                              ❌ Blocked      ✓ Works       ✓ Works
                              (0 results)    (10 results)  (20 results)
```

**Pros:** Fast, low resource
**Cons:** Engines block raw HTTP requests

---

### 2. fcrawl gsearch (Google-only Camoufox)

```
User Terminal
     │
     ▼
fcrawl CLI ──→ Camoufox Browser ──→ google.com/search
              (real Firefox)              │
                                          ▼
                                    ✓ Works reliably
                                    (appears as human)
```

**Pros:** Reliable, no blocking
**Cons:** Google only, slower

---

### 3. fcrawl csearch (Multi-engine Camoufox) [PLANNED]

```
User Terminal
     │
     ▼
fcrawl CLI ──→ Camoufox Browser
              (real Firefox)
                    │
         ┌─────────┼─────────┬─────────┐
         ▼         ▼         ▼         ▼
      Tab 1     Tab 2     Tab 3     Tab 4
      Google     Bing      DDG      Brave
         │         │         │         │
         ▼         ▼         ▼         ▼
    ✓ Works   ✓ Works   ✓ Works   ✓ Works
         │         │         │         │
         └─────────┴─────────┴─────────┘
                       │
                       ▼
              Aggregate & Rank
              (like SearXNG does)
                       │
                       ▼
              Return merged results
```

**Pros:** Reliable, multi-engine, no blocking
**Cons:** Slower, more resources

---

## When to Use Each

| Scenario | Recommended Command |
|----------|---------------------|
| Quick search, don't care about reliability | `fcrawl search` |
| Need reliable Google results | `fcrawl gsearch` |
| Need reliable multi-engine results | `fcrawl csearch` (planned) |
| Debugging engine issues | `fcrawl search --debug` |

---

## Data Flow Summary

```
                    ┌─────────────────────────────────────────┐
                    │              USER REQUEST               │
                    │     "python tutorials" --limit 20       │
                    └─────────────────────┬───────────────────┘
                                          │
              ┌───────────────────────────┼───────────────────────────┐
              │                           │                           │
              ▼                           ▼                           ▼
    ┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
    │  fcrawl search  │        │ fcrawl gsearch  │        │ fcrawl csearch  │
    │                 │        │                 │        │   (planned)     │
    │  SearXNG-based  │        │  Google-only    │        │  Multi-engine   │
    │  via Firecrawl  │        │  Camoufox       │        │  Camoufox       │
    └────────┬────────┘        └────────┬────────┘        └────────┬────────┘
             │                          │                          │
             ▼                          ▼                          ▼
    ┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
    │   HTTP Client   │        │    Camoufox     │        │    Camoufox     │
    │  (firecrawl-py) │        │  (Playwright)   │        │  (Playwright)   │
    └────────┬────────┘        └────────┬────────┘        └────────┬────────┘
             │                          │                          │
             ▼                          ▼                          ▼
    ┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
    │  Firecrawl API  │        │  google.com     │        │ google.com      │
    │  (Docker)       │        │                 │        │ bing.com        │
    │       │         │        │                 │        │ duckduckgo.com  │
    │       ▼         │        │                 │        │ brave.com       │
    │    SearXNG      │        │                 │        │                 │
    │   (Docker)      │        │                 │        │                 │
    └────────┬────────┘        └────────┬────────┘        └────────┬────────┘
             │                          │                          │
             ▼                          ▼                          ▼
    ┌─────────────────┐        ┌─────────────────┐        ┌─────────────────┐
    │  Raw HTTP to    │        │  Real browser   │        │  Real browser   │
    │  search engines │        │  to Google      │        │  to all engines │
    │                 │        │                 │        │                 │
    │  ⚠️ BLOCKED     │        │  ✓ WORKS        │        │  ✓ WORKS        │
    └─────────────────┘        └─────────────────┘        └─────────────────┘
```

---

## Implementation Status

| Command | Status | Reliability | Speed |
|---------|--------|-------------|-------|
| `fcrawl search` | ✅ Implemented | ⚠️ Medium (engines block) | Fast |
| `fcrawl gsearch` | ✅ Implemented | ✅ High | Medium |
| `fcrawl csearch` | 📋 Planned | ✅ High (expected) | Medium |

---

## Related Documents

- [SearXNG Reliability & Debug Mode](../SEARXNG_RELIABILITY_AND_DEBUG.md)
- [csearch Multi-Engine Plan](./CSEARCH_MULTI_ENGINE_CAMOUFOX.md)
