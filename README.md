# fcrawl

A CLI tool that gives AI coding agents (Claude Code, etc.) the ability to interact with the web — scraping pages, searching Google, reading YouTube videos, browsing Reddit and X/Twitter, and transcribing audio. Built on top of a self-hosted [Firecrawl](https://github.com/mendableai/firecrawl) instance.

Designed to replace built-in web tools (WebSearch, WebFetch, Firecrawl MCP) with something simpler, more reliable, and far more capable. An AI agent with `fcrawl` in its Bash tool can scrape any page, search the web, "watch" YouTube videos via transcripts, research Reddit threads, read tweets and threads, and transcribe audio files — all through one consistent CLI.

## For AI agents

If you're configuring an AI coding agent to use fcrawl, copy the contents of [`AGENT_GUIDE.md`](./AGENT_GUIDE.md) into your `CLAUDE.md` (or equivalent system instructions). It contains quick-reference patterns for search, scraping, YouTube, and X/Twitter — written specifically for how agents should use each command.

## Installation

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
# Install as a global CLI tool
cd firecrawl-workspace/scripts/fcrawl
uv tool install -e .

# Or run directly from the project
uv run fcrawl --help
```

For local audio transcription (optional):

```bash
uv pip install -e ".[asr]"
```

This pulls in PyTorch, FunASR, and OpenCC — heavy deps that are only needed for `transcribe` and `yt-transcript --force-transcribe`.

Browser-based search (`csearch`, `gsearch`) requires a one-time browser download:

```bash
python -m camoufox fetch
```

## Configuration

fcrawl talks to a self-hosted Firecrawl at `http://localhost:3002` by default. No API key needed for localhost.

### Config file

Create `~/.fcrawlrc`:

```json
{
  "api_url": "http://localhost:3002",
  "api_key": null,
  "serper_api_key": "your-serper-key",
  "default_format": "markdown",
  "cache_enabled": false,
  "cache_duration": 3600,
  "yt_cookies_from_browser": "chrome",
  "yt_cookies_file": null
}
```

A `.fcrawlrc` in the current directory takes priority over `~/.fcrawlrc`.

### Environment variables

These override the config file:

| Variable | Purpose |
|----------|---------|
| `FIRECRAWL_API_URL` | Firecrawl API endpoint |
| `FIRECRAWL_API_KEY` | API key (not needed for localhost) |
| `SERPER_API_KEY` | Serper.dev key for `search` command |
| `FCRAWL_YT_COOKIES_FILE` | Netscape cookie file for YouTube |
| `FCRAWL_YT_COOKIES_FROM_BROWSER` | Browser to pull YouTube cookies from (e.g., `chrome`) |

## Commands

### `scrape` — Scrape a single page

```bash
fcrawl scrape <url> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--format` | `-f` | Output format: `markdown`, `html`, `links`, `screenshot`, `extract`. Repeatable. |
| `--output` | `-o` | Save to file |
| `--copy` | | Copy to clipboard |
| `--json` | | Output as JSON |
| `--pretty/--no-pretty` | | Pretty print (default: on) |
| `--include` | `-i` | CSS selectors to include. Repeatable. |
| `--exclude` | `-e` | CSS selectors to exclude. Repeatable. |
| `--article` | | Article mode — aggressive filtering for clean article text |
| `--raw` | | Raw mode — disable all content filtering |
| `--no-links` | | Strip markdown links, keep display text |
| `--wait` | | Wait N milliseconds before scraping (for JS-heavy pages) |
| `--screenshot-full` | | Full-page screenshot instead of viewport |
| `--no-cache` | | Bypass cache, force fresh fetch |
| `--cache-only` | | Only return cached results, don't hit the API |

```bash
# Basic scrape
fcrawl scrape https://example.com

# Clean article extraction (strips nav, ads, comments)
fcrawl scrape https://blog.com/post --article --no-links

# Get all links from a page
fcrawl scrape https://docs.site.com -f links

# CSS filtering
fcrawl scrape https://site.com -i ".main-content" -e ".sidebar" -e ".ads"

# Wait for JS to render
fcrawl scrape https://spa-site.com --raw --wait 5000

# Save to file
fcrawl scrape https://example.com -o page.md
```

Content mode tips:
- `--article` is great for blog posts and news. Don't use it for API docs — it filters too aggressively.
- `--raw` disables server-side filtering. Use with `--wait` for JS-heavy pages.
- `-f links` extracts links client-side from the scraped markdown.

---

### `search` — Web search via Serper (Google API)

Requires `SERPER_API_KEY` or `serper_api_key` in config.

```bash
fcrawl search <query> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--limit` | `-l` | Max results (default: 20) |
| `--locale` | `-L` | Locale code, e.g. `zh-TW`, `ja-JP`, `en-GB` |
| `--location` | | Geographic bias, e.g. `"Taipei, Taiwan"` |
| `--output` | `-o` | Save to file |
| `--json` | | Output as JSON |
| `--pretty/--no-pretty` | | Pretty print |
| `--no-cache` | | Bypass cache |
| `--cache-only` | | Only return cached results |
| `--debug` | | Show provider stats (source, timing, pagination) |

```bash
fcrawl search "python web scraping"
fcrawl search "AI news" -l 30
fcrawl search "restaurants" -L zh-TW --location "Taipei, Taiwan"
fcrawl search "site:github.com firecrawl"   # Domain targeting via query
fcrawl search "LLM benchmark" --debug
```

Supports automatic pagination — if `--limit` exceeds 100, it fetches multiple pages and deduplicates.

---

### `csearch` — Multi-engine browser search

Searches Google, Bing, and Brave in parallel using an anti-detection browser ([Camoufox](https://github.com/nickspaargaren/camoufox)). Results are aggregated and deduplicated — URLs found by multiple engines rank higher.

```bash
fcrawl csearch <query> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--engine` | `-e` | Engine: `google`, `bing`, `brave`, `all` (default: `all`). Also comma-separated. |
| `--limit` | `-l` | Max results (default: 20) |
| `--locale` | `-L` | Locale code |
| `--headful` | | Show browser window (debugging) |
| `--debug` | | Show per-engine stats and aggregation breakdown |
| `--parallel/--sequential` | | Run engines in parallel (default) or one at a time |
| `--output` | `-o` | Save to file |
| `--json` | | Output as JSON |
| `--no-cache` | | Bypass cache |
| `--cache-only` | | Only return cached results |

```bash
fcrawl csearch "python tutorials"                # All 3 engines
fcrawl csearch "python tutorials" -e google      # Google only
fcrawl csearch "AI news" --debug                 # Show engine stats
fcrawl csearch "news" -L ja-JP                   # Japanese results
```

---

### `gsearch` — Direct Google browser search

Like `csearch` but Google-only. Simpler, slightly faster for single-engine use.

```bash
fcrawl gsearch <query> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--limit` | `-l` | Max results (default: 10) |
| `--locale` | `-L` | Locale code |
| `--headful` | | Show browser window |
| `--output` | `-o` | Save to file |
| `--json` | | Output as JSON |
| `--no-cache` | | Bypass cache |
| `--cache-only` | | Only return cached results |

```bash
fcrawl gsearch "site:github.com firecrawl" -l 20
fcrawl gsearch "Claude Code" --headful    # Watch it search
```

---

### `crawl` — Crawl multiple pages

Crawls a website and saves each page as a separate `.md` file in an output directory.

```bash
fcrawl crawl <url> [options]
```

| Option | Description |
|--------|-------------|
| `--limit` | Max pages to crawl (default: 10) |
| `--depth` | Max crawl depth |
| `--output`, `-o` | Output directory (default: `crawl-{domain}-{date}`) |
| `--include-paths` | Only crawl URLs matching these patterns. Repeatable. |
| `--exclude-paths` | Skip URLs matching these patterns. Repeatable. |
| `--no-links` | Strip markdown links from output |
| `--poll-interval` | Polling interval in seconds (default: 2) |
| `--timeout` | Timeout in seconds (default: 300) |
| `--no-cache` | Bypass cache |
| `--cache-only` | Only return cached results |

```bash
fcrawl crawl https://blog.com --limit 20
fcrawl crawl https://docs.site.com --depth 2 -o ./my-docs/
fcrawl crawl https://site.com --exclude-paths "/admin/*" "/private/*"
```

Note: Crawl relies on Firecrawl's link discovery, which may not work on JS-heavy sites. Workaround:
1. `fcrawl scrape URL --raw --wait 5000 -f links` — discover links
2. `fcrawl scrape` each link individually

---

### `map` — Discover URLs on a site

```bash
fcrawl map <url> [options]
```

| Option | Description |
|--------|-------------|
| `--search` | Filter results server-side by keyword |
| `--limit` | Limit number of URLs |
| `--include-subdomains` | Include subdomains |
| `--output`, `-o` | Save to file |
| `--json` | Output as JSON |

```bash
fcrawl map https://docs.site.com
fcrawl map https://docs.site.com --search "api" --limit 50
```

---

### `extract` — AI-powered structured extraction

> **Not working.** Requires RabbitMQ which is currently disabled. Use `fcrawl scrape` and process the content instead.

```bash
fcrawl extract <urls...> [options]
```

| Option | Description |
|--------|-------------|
| `--prompt` | Extraction prompt |
| `--fields` | Comma-separated field names |
| `--schema` | Path to JSON schema file |
| `--output`, `-o` | Save to file |
| `--json` | Output as JSON |

---

### `yt-transcript` — YouTube video transcripts

Downloads subtitles from YouTube videos. Falls back to local ASR transcription (SenseVoice) when no subtitles are available.

```bash
fcrawl yt-transcript <url> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--lang` | `-l` | Preferred language code (e.g., `en`, `zh-Hant`) |
| `--list-langs` | | List available subtitle languages |
| `--force-transcribe` | `-t` | Skip subtitles, force local ASR transcription |
| `--no-transcribe` | | Disable ASR fallback (fail if no subtitles) |
| `--simplified` | | Output Simplified Chinese (default: Traditional) |
| `--cookies` | | Netscape cookie file for YouTube |
| `--cookies-from-browser` | | Browser to extract cookies from (e.g., `chrome`, `firefox:Profile`) |
| `--cookies-on-fail/--no-cookies-on-fail` | | Auto-retry with cookies on 429/block (default: on) |
| `--output` | `-o` | Save to file |
| `--copy` | | Copy to clipboard |
| `--json` | | Output as JSON |
| `--quiet` | `-q` | Suppress progress |

```bash
# Get transcript
fcrawl yt-transcript "https://youtube.com/watch?v=VIDEO_ID"

# Specific language
fcrawl yt-transcript "https://youtu.be/VIDEO_ID" -l zh-Hant

# List available languages
fcrawl yt-transcript "https://youtube.com/watch?v=VIDEO_ID" --list-langs

# Force local transcription (ignore YouTube subtitles)
fcrawl yt-transcript URL -t

# Use browser cookies for age-restricted/throttled content
fcrawl yt-transcript URL --cookies-from-browser chrome
```

Language selection priority: preferred language > video's original language > English > first available.

Cookie config can also be set in `~/.fcrawlrc` or via `FCRAWL_YT_COOKIES_*` env vars so you don't need to pass flags every time.

---

### `yt-channel` — Explore YouTube channels

```bash
fcrawl yt-channel <channel> [options]
```

Channel can be `@handle`, full URL, or channel ID (`UC...`).

| Option | Short | Description |
|--------|-------|-------------|
| `--limit` | `-n` | Number of videos (default: 20, 0 = all) |
| `--sort` | `-s` | Sort: `recency`, `views`, `duration`, `duration_asc` |
| `--search` | `-q` | Filter by title keyword |
| `--type` | `-t` | Content type: `videos`, `shorts`, `streams` |
| `--with-dates` | | Include upload dates (slower) |
| `--ids-only` | | Output video IDs only (one per line, pipe-friendly) |
| `--output` | `-o` | Save to file |
| `--json` | | Output as JSON |
| `--quiet` | | Suppress progress |

```bash
fcrawl yt-channel "@3Blue1Brown"
fcrawl yt-channel "@3Blue1Brown" --sort views -n 10     # Top 10 by views
fcrawl yt-channel "@3Blue1Brown" --search "linear"      # Filter by keyword
fcrawl yt-channel "@3Blue1Brown" --type shorts           # List shorts
fcrawl yt-channel "@3Blue1Brown" --ids-only | head -5    # Pipe video IDs
```

---

### `transcribe` — Local audio/video transcription

Transcribe audio or video files locally using SenseVoice ASR. No API calls, runs entirely on your machine.

Requires the `[asr]` optional dependencies (`uv pip install -e ".[asr]"`).

Supported formats: wav, mp3, m4a, flac, ogg, mp4, mkv, webm.

```bash
fcrawl transcribe <file> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--model` | `-m` | Model: `sensevoice` (default), `paraformer`, `fun-asr-nano` |
| `--lang` | `-l` | Language hint: `auto`, `zh`, `en`, `ja`, `ko`, etc. |
| `--format` | `-f` | Output: `txt` (default), `srt`, `vtt`, `json` |
| `--simplified` | | Output Simplified Chinese (default: Traditional) |
| `--output` | `-o` | Save to file |
| `--copy` | | Copy to clipboard |
| `--json` | | Output as JSON with metadata |
| `--quiet` | `-q` | Suppress progress |

```bash
fcrawl transcribe recording.mp3
fcrawl transcribe video.mp4 -o transcript.txt
fcrawl transcribe podcast.mp3 -f srt -o subtitles.srt
fcrawl transcribe audio.wav --json     # Includes duration, RTF, model info
```

Models:
- **sensevoice** — ~234M params, multilingual, fast. Default choice.
- **paraformer** — ~220M params, Chinese-focused, CPU only.
- **fun-asr-nano** — ~800M params, 31 languages, highest quality.

Auto-detects MPS (Apple Silicon), CUDA, or CPU.

---

### `reddit` — Reddit research workflow

Read Reddit with no auth using Reddit's public `.json` endpoints.

#### `reddit search`

```bash
fcrawl reddit search <query> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--subreddit` | `-s` | Restrict to one subreddit |
| `--user` | `-u` | Restrict to posts by a specific author |
| `--sort` | | Sort: `relevance`, `hot`, `top`, `new`, `comments` |
| `--time` | `-t` | Time filter: `hour`, `day`, `week`, `month`, `year`, `all` |
| `--limit` | `-l` | Max results (default: 20, max: 100) |
| `--after` | | Pagination cursor from previous response |
| `--output` | `-o` | Save output to file |
| `--json` | | Output as JSON |
| `--pretty/--no-pretty` | | Pretty terminal output |
| `--no-cache` | | Bypass cache |
| `--cache-only` | | Only return cached results |

```bash
fcrawl reddit search "claude code"
fcrawl reddit search "mcp" -s Python -l 10
fcrawl reddit search "agent tooling" -u spez --sort top --time month
```

#### `reddit post`

```bash
fcrawl reddit post <url_or_id> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--sort` | | Comment sort: `best`, `top`, `new`, `controversial`, `old`, `qa` |
| `--limit` | `-l` | Max top-level comments (default: 20, `0` = all) |
| `--depth` | `-d` | Max reply depth (default: 3) |
| `--no-comments` | | Fetch post only |
| `--output` | `-o` | Save output to file |
| `--json` | | Output as JSON |
| `--pretty/--no-pretty` | | Pretty terminal output |
| `--no-cache` | | Bypass cache |
| `--cache-only` | | Only return cached results |

```bash
fcrawl reddit post https://reddit.com/r/python/comments/abc123/post-title/
fcrawl reddit post abc123 --sort top --limit 5
fcrawl reddit post abc123 --no-comments
```

#### `reddit subreddit`

```bash
fcrawl reddit subreddit <name> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--sort` | | Feed sort: `hot`, `new`, `top`, `rising` |
| `--time` | `-t` | Time filter for `--sort top` |
| `--limit` | `-l` | Max posts (default: 20, max: 100) |
| `--after` | | Pagination cursor from previous response |
| `--about` | | Show subreddit metadata instead of feed |
| `--output` | `-o` | Save output to file |
| `--json` | | Output as JSON |
| `--pretty/--no-pretty` | | Pretty terminal output |
| `--no-cache` | | Bypass cache |
| `--cache-only` | | Only return cached results |

```bash
fcrawl reddit subreddit python
fcrawl reddit subreddit r/Python --sort top --time week
fcrawl reddit subreddit python --about
```

#### `reddit user`

```bash
fcrawl reddit user <username> [options]
```

| Option | Description |
|--------|-------------|
| `--about` | Show profile info only |
| `--posts-only` | Show only submitted posts |
| `--comments-only` | Show only comments |
| `--type` | Activity type: `overview`, `submitted`, `comments` |
| `--sort` | Sort: `hot`, `new`, `top`, `controversial` |
| `--time`, `-t` | Time filter for `--sort top` |
| `--limit`, `-l` | Max items (default: 20, max: 100) |
| `--after` | Pagination cursor from previous response |
| `--output`, `-o` | Save output to file |
| `--json` | Output as JSON |
| `--pretty/--no-pretty` | Pretty terminal output |
| `--no-cache` | Bypass cache |
| `--cache-only` | Only return cached results |

```bash
fcrawl reddit user spez
fcrawl reddit user u/spez --posts-only --sort top
fcrawl reddit user spez --comments-only -l 50
```

---

### `x` — X/Twitter

Search tweets, fetch profiles, read threads, and manage accounts. Uses a vendored [twscrape](https://github.com/vladkens/twscrape) library with SQLite-backed account management.

#### `x search`

```bash
fcrawl x search <query> [options]
```

| Option | Short | Description |
|--------|-------|-------------|
| `--limit` | `-l` | Max results (default: 20) |
| `--sort` | | Sort: `top`, `latest`, `photos`, `videos` |
| `--output` | `-o` | Save to file |
| `--json` | | Output as JSON |

```bash
fcrawl x search "Claude Code" --sort latest
fcrawl x search "AI news" -l 50 --json
```

#### `x tweet`

```bash
fcrawl x tweet <id_or_url> [options]
```

| Option | Description |
|--------|-------------|
| `--thread` | Fetch full thread (author's replies in the conversation) |
| `--with-replies` | Include replies from other users (requires `--thread`) |
| `--reply-limit` | Max replies to fetch (default: 30) |
| `--output`, `-o` | Save to file |
| `--json` | Output as JSON |

```bash
fcrawl x tweet https://x.com/user/status/1234567890
fcrawl x tweet 1234567890 --thread
fcrawl x tweet 1234567890 --thread --with-replies
```

Auto-detects and displays X Articles (long-form content) attached to tweets.

#### `x article`

Fetch X long-form articles (Draft.js rich text).

```bash
fcrawl x article <id_or_url> [options]
```

| Option | Description |
|--------|-------------|
| `--output`, `-o` | Save to file |
| `--json` | Output as JSON |
| `--raw` | Output raw Draft.js blocks instead of markdown |

```bash
fcrawl x article https://x.com/user/status/123 -o article.md
```

#### `x user`

```bash
fcrawl x user <handle> [--json] [-o file]
```

```bash
fcrawl x user anthropic
fcrawl x user elonmusk --json
```

#### `x tweets`

```bash
fcrawl x tweets <handle> [-l limit] [--json] [-o file]
```

```bash
fcrawl x tweets anthropic --limit 50
```

#### `x accounts`

Manage X/Twitter accounts used for API authentication.

```bash
fcrawl x accounts                                    # List all accounts
fcrawl x accounts add credentials.txt                # Add from file
fcrawl x accounts add credentials.txt -f "user:pass" # Custom format
fcrawl x accounts login                              # Login all inactive
fcrawl x accounts login specific_user                # Login one account
fcrawl x accounts reset                              # Reset rate limit locks
fcrawl x accounts add-tokens --ct0 VALUE --auth VALUE  # Add via browser cookies
```

Accounts are stored in `~/.config/fcrawl/x_accounts.db` (SQLite).

The easiest way to add an account is `add-tokens` — grab `ct0` and `auth_token` cookies from your browser's dev tools while logged into X.

---

### `config` — View current configuration

```bash
fcrawl config
```

Shows all active config values and their sources.

## Caching

fcrawl caches API responses in `/tmp/fcrawl-cache/` as JSON files, keyed by a SHA-256 hash of the URL/query + options.

Commands with caching: `scrape`, `crawl`, `search`, `csearch`, `gsearch`, `reddit`.

| Flag | Effect |
|------|--------|
| `--no-cache` | Skip cache, always fetch fresh |
| `--cache-only` | Only return cached results, don't make API calls |

No flags = read from cache if available, otherwise fetch and cache the result.

## Common flags

These work across most commands:

| Flag | Short | Description |
|------|-------|-------------|
| `--output` | `-o` | Save output to file |
| `--json` | | Output as JSON (for scripting) |
| `--copy` | | Copy to clipboard |
| `--pretty/--no-pretty` | | Pretty terminal output (default: on). Auto-disables when piped. |

When stdout is not a TTY (piped to another command), output automatically switches to plain text.

## Scripting and piping

```bash
# Get plain URLs from search (auto-detects pipe)
fcrawl search "python tutorials" --no-pretty | head -5

# Pipe search results into scrape
fcrawl search "query" --no-pretty | xargs -I{} fcrawl scrape {}

# Extract links then scrape each
fcrawl scrape https://docs.site.com -f links --no-pretty | xargs -I{} fcrawl scrape {} -o docs/{}.md

# YouTube: get top 5 video IDs from a channel, then transcript each
fcrawl yt-channel "@channel" --ids-only | head -5 | \
  xargs -I{} fcrawl yt-transcript "https://youtube.com/watch?v={}" -o transcripts/{}.txt

# JSON processing with jq
fcrawl scrape https://example.com --json | jq '.metadata.title'
fcrawl x tweets anthropic --json | jq '.[].rawContent'
```

## Project structure

```
fcrawl/
├── pyproject.toml                  # Package config, deps, entry point
├── src/fcrawl/
│   ├── __init__.py                 # Exports cli, version
│   ├── cli.py                      # Click CLI group, registers all commands
│   ├── commands/
│   │   ├── scrape.py               # Single URL scraping
│   │   ├── crawl.py                # Multi-page crawling
│   │   ├── map.py                  # URL discovery
│   │   ├── extract.py              # AI extraction (broken)
│   │   ├── search.py               # Serper.dev search
│   │   ├── csearch.py              # Multi-engine browser search
│   │   ├── gsearch.py              # Google browser search
│   │   ├── reddit.py               # Reddit commands (search/post/subreddit/user)
│   │   ├── x.py                    # X/Twitter commands
│   │   ├── yt_transcript.py        # YouTube transcripts
│   │   ├── yt_channel.py           # YouTube channel explorer
│   │   └── transcribe.py           # Local ASR transcription
│   ├── utils/
│   │   ├── config.py               # Config loading, Firecrawl client
│   │   ├── output.py               # Display, save, clipboard
│   │   ├── cache.py                # File-based response caching
│   │   ├── reddit_client.py        # Reddit .json HTTP client
│   │   ├── x_client.py             # twscrape client setup
│   │   ├── article_parser.py       # X Article Draft.js-to-markdown
│   │   └── transcriber.py          # SenseVoice ASR engine
│   ├── engines/
│   │   ├── base.py                 # Search engine ABC + shared logic
│   │   ├── google.py               # Google SERP scraper
│   │   ├── bing.py                 # Bing SERP scraper
│   │   ├── brave.py                # Brave SERP scraper
│   │   └── aggregator.py           # Multi-engine result dedup/ranking
│   └── vendors/
│       └── twscrape/               # Vendored X/Twitter API library
├── searxng/                        # SearXNG config (legacy, optional)
└── docs/                           # Investigation notes
```

## Dependencies

Core (always installed):

| Package | Purpose |
|---------|---------|
| click | CLI framework |
| rich | Terminal formatting |
| firecrawl-py | Firecrawl Python SDK |
| pyperclip | Clipboard |
| python-dotenv | .env loading |
| beautifulsoup4 | HTML parsing |
| httpx | HTTP client |
| requests | HTTP client (Serper API) |
| camoufox[geoip] | Anti-detection browser for csearch/gsearch |
| fake-useragent | UA spoofing |
| yt-dlp | YouTube downloading |
| aiosqlite | Async SQLite (twscrape account DB) |
| loguru | Logging (twscrape) |
| orjson | Fast JSON (twscrape) |
| pyotp | 2FA (twscrape login) |

Optional `[asr]` group (for `transcribe` and `yt-transcript` ASR fallback):

| Package | Purpose |
|---------|---------|
| funasr | Alibaba's ASR framework |
| torch, torchaudio | Model inference |
| pydub | Audio format conversion |
| opencc | Simplified-to-Traditional Chinese |

## Troubleshooting

**Connection refused on scrape/crawl/map:**
Firecrawl needs to be running. Start it with `docker compose up -d` in the firecrawl repo root.

**`search` returns nothing:**
Check that `SERPER_API_KEY` is set in your env or `~/.fcrawlrc`.

**`csearch`/`gsearch` fails with "browser not found":**
Run `python -m camoufox fetch` to download the browser binary.

**YouTube 429 / throttled:**
Pass `--cookies-from-browser chrome` or set `yt_cookies_from_browser` in `~/.fcrawlrc`.

**X/Twitter "no accounts":**
Add an account first: `fcrawl x accounts add-tokens --ct0 VALUE --auth VALUE`.

**`extract` not working:**
This is expected — RabbitMQ is disabled. Use `fcrawl scrape` and process the content yourself.

## License

MIT
