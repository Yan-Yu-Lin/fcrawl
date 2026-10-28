# fcrawl CLI

A command-line interface for Firecrawl, designed for use with a self-hosted Firecrawl instance.

## Project Structure

```
fcrawl/
├── fcrawl.py              # Main entry point, CLI group definition
├── commands/              # Command implementations
│   ├── scrape.py          # Single URL scraping
│   ├── crawl.py           # Multi-page website crawling
│   ├── map.py             # URL discovery/sitemap mapping
│   ├── extract.py         # AI-powered structured extraction
│   └── search.py          # Web search with optional scraping
├── utils/
│   ├── config.py          # Configuration loading, Firecrawl client creation
│   └── output.py          # Output handling (display, save, clipboard)
├── searxng/               # SearXNG config (optional search backend)
├── requirements.txt       # Python dependencies
└── install.sh             # Quick install script
```

## Commands

| Command | Purpose |
|---------|---------|
| `scrape <url>` | Scrape single URL (markdown, html, links, screenshot, extract) |
| `crawl <url>` | Crawl website with depth/limit controls |
| `map <url>` | Discover all URLs on a domain |
| `extract <urls>` | Extract structured data using AI (requires --prompt/--fields/--schema) |
| `search <query>` | Fast Google web search via Serper API |
| `dev <query>` | Code search: GitHub issues, PRs, READMEs, docs (Firecrawl Developer Index) |
| `csearch <query>` | Multi-engine browser search (Google/Bing/Brave via Camoufox) |
| `gsearch <query>` | Direct Google browser search via Camoufox |
| `dcard post <url_or_id>` | Dcard post + comments via Dcard's JSON API (through Firecrawl) |
| `cache stats\|clean\|clear` | Inspect the /tmp/fcrawl-cache store; `clean --yes` trashes junk (challenge pages, 4xx, empty) |
| `config` | View current configuration (keys masked) |

## Running Commands

```bash
# Run via installed entrypoint
fcrawl <command> [options]

# Or via uv
uv run fcrawl <command> [options]

# Examples
fcrawl scrape https://example.com -f markdown
fcrawl crawl https://example.com --limit 5 --depth 2
fcrawl map https://example.com --limit 50
fcrawl search "python tutorials" -l 10
fcrawl csearch "python tutorials" -l 20
```

## Configuration

### Defaults (in `utils/config.py`)

```python
DEFAULT_CONFIG = {
    'api_url': 'http://localhost:3002',  # Self-hosted Firecrawl
    'api_key': None,                      # Not required for localhost
    'api_keys': [],                       # Extra keys, ONE PER FIRECRAWL ACCOUNT (key pool)
    'rate_limit_per_minute': 10,          # Per-account /scrape budget (Free tier)
    'max_wait': 75,                       # Seconds to wait when every key is rate limited
    'jina_fallback': True,                # Jina Reader relay after Firecrawl fails
    'serper_api_key': None,               # For `search` command (Serper.dev)
    'default_format': 'markdown',
    'cache_ttl_days': 7,                  # 0 = cache never expires
}
```

### Key pool (`utils/keypool.py`)

Firecrawl rate limits are per *team*, so `api_keys` only helps when each key belongs to a
different account. All fcrawl processes share `/tmp/fcrawl-keys/state.json` (flock'd; stores
key hashes, not keys): each call picks the key with the fewest requests in the last 60 s, a 429
puts that key on cooldown for `Retry-After` seconds and the call retries on another key, and when
every key is cooling down the call sleeps (bounded by `max_wait` / `--max-wait`). 401/402 disable a
key for the rest of the process lifetime in that state file. `scrape`, `crawl`, `map`, `extract`
and `dcard` all go through `firecrawl_call()` / `get_key_pool()`; `dev` still uses `api_key` only.

### Scrape relay + validation (`commands/scrape.py`, `utils/cache.py`)

Firecrawl first (429 never escapes the pool). Only if Firecrawl errors, times out, or returns a
page that fails `validate_result()` (status >= 400, "Just a moment..."-style titles, challenge
phrases, empty body) does Jina Reader run — in JSON mode so its `httpStatus` is visible. Both
providers' results must pass `validate_result()` before being printed or cached; otherwise the
command aborts with `Blocked by the target site` and `--save-temp` prints `RESULT … status=blocked`.
Cache entries expire after `cache_ttl_days`; junk entries are skipped on read.

### Configuration Sources (priority order)

1. Environment variables (highest priority):
   - `FIRECRAWL_API_URL`
   - `FIRECRAWL_API_KEY`
   - `FIRECRAWL_API_KEYS` (comma-separated, replaces `api_keys`)
   - `SERPER_API_KEY` (for `search` command)
   - `FCRAWL_YT_COOKIES_FILE` (for `yt-transcript`, Netscape cookies.txt)
   - `FCRAWL_YT_COOKIES_FROM_BROWSER` (for `yt-transcript`, e.g. `chrome`)

2. Config file: `.fcrawlrc` (current dir) or `~/.fcrawlrc`
   ```json
   {
     "api_url": "http://localhost:3002",
     "api_key": "fc-account-one",
     "api_keys": ["fc-account-one", "fc-account-two"],
     "serper_api_key": "optional-serper-key",
     "yt_cookies_file": "optional-netscape-cookies.txt",
     "yt_cookies_from_browser": "chrome"
   }
   ```

3. Default config (lowest priority)

### Localhost Detection

The client auto-uses a dummy API key for localhost instances since self-hosted Firecrawl doesn't require authentication.

## Dependencies

- **click**: CLI framework
- **rich**: Terminal formatting (tables, progress, markdown)
- **firecrawl-py**: Official Firecrawl Python SDK
- **pyperclip**: Clipboard support
- **python-dotenv**: Environment variable loading

## Development Notes

- Uses Click for command parsing with `@click.group()` pattern
- Rich provides progress spinners/bars and formatted output
- TTY detection enables plain output for Unix pipelines (`--no-pretty` or piped output)
- All commands follow consistent error handling: catch exception, stop progress, print error, abort
- The `search.py` command is the most complex (multiple sources, optional scraping, time filters)
- Firecrawl SDK methods used: `scrape()`, `crawl()`, `map()`, `extract()`, `search()`

## Testing

No formal test suite. Manual testing against a running Firecrawl instance:

```bash
# Ensure Firecrawl is running on localhost:3002
# Then run commands manually
python fcrawl.py scrape https://example.com
python fcrawl.py quick https://example.com
```
