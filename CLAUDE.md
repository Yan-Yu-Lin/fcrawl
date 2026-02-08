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
| `csearch <query>` | Multi-engine browser search (Google/Bing/Brave via Camoufox) |
| `gsearch <query>` | Direct Google browser search via Camoufox |
| `config` | View current configuration |

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
    'serper_api_key': None,               # For `search` command (Serper.dev)
    'default_format': 'markdown',
    'cache_enabled': False,
}
```

### Configuration Sources (priority order)

1. Environment variables (highest priority):
   - `FIRECRAWL_API_URL`
   - `FIRECRAWL_API_KEY`
   - `SERPER_API_KEY` (for `search` command)
   - `FCRAWL_YT_COOKIES_FILE` (for `yt-transcript`, Netscape cookies.txt)
   - `FCRAWL_YT_COOKIES_FROM_BROWSER` (for `yt-transcript`, e.g. `chrome`)

2. Config file: `.fcrawlrc` (current dir) or `~/.fcrawlrc`
   ```json
   {
     "api_url": "http://localhost:3002",
     "api_key": "optional-key",
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
