# fcrawl - Firecrawl CLI Tool

A powerful command-line interface for Firecrawl, making web scraping as easy as using `curl`.

## Features

- 🚀 **Simple by default** - Just run `fcrawl scrape URL` to get markdown content
- 🔍 **Web search** - Fast Google search via Serper (`search`) plus multi-engine browser search (`csearch`)
- 📦 **Multiple formats** - Markdown, HTML, links, screenshots, and structured data
- 🔄 **Batch operations** - Crawl entire websites or map site structures
- 📋 **Clipboard support** - Copy results directly to clipboard
- 🎨 **Beautiful output** - Syntax highlighting and formatted tables in terminal
- 💾 **Flexible saving** - Save to files or pipe to other tools
- ⚙️ **Configurable** - Set defaults in config file

## Installation

### Quick Install

```bash
cd firecrawl-workspace/scripts/fcrawl
./install.sh
```

### Manual Install

1. Install dependencies:
```bash
pip install --user --break-system-packages click rich pyperclip python-dotenv firecrawl-py
```

2. Create symlink:
```bash
ln -s $(pwd)/fcrawl.py ~/.local/bin/fcrawl
```

3. Ensure `~/.local/bin` is in your PATH

## Configuration

fcrawl uses your local Firecrawl instance at `http://localhost:3002` by default.

Create `~/.fcrawlrc` to customize:
```json
{
  "api_url": "http://localhost:3002",
  "default_format": "markdown",
  "cache_enabled": false
}
```

Or use environment variables:
```bash
export FIRECRAWL_API_URL=http://localhost:3002
export FIRECRAWL_API_KEY=your-key  # Only if needed
```

## Usage

### Basic Scraping

```bash
# Simple scrape (markdown by default)
fcrawl scrape https://example.com

# Save to file
fcrawl scrape https://example.com -o output.md

# Copy to clipboard
fcrawl scrape https://example.com --copy

# Multiple formats
fcrawl scrape https://example.com -f markdown -f links
```

### YouTube Transcripts

```bash
# Download a transcript
fcrawl yt-transcript "https://www.youtube.com/watch?v=VIDEO_ID"

# Pick a language
fcrawl yt-transcript "https://www.youtube.com/watch?v=VIDEO_ID" -l en

# List available languages (metadata only; does not download subs)
fcrawl yt-transcript "https://www.youtube.com/watch?v=VIDEO_ID" --list-langs
```

If YouTube rate-limits unauthenticated requests (HTTP 429), you can provide cookies:

```bash
# Load cookies from your local browser (recommended)
fcrawl yt-transcript "https://www.youtube.com/watch?v=VIDEO_ID" --cookies-from-browser chrome

# Or use a Netscape cookie file
fcrawl yt-transcript "https://www.youtube.com/watch?v=VIDEO_ID" --cookies "$HOME/Downloads/youtube-cookies.txt"
```

You can also set defaults via `~/.fcrawlrc`:

```json
{
  "yt_cookies_from_browser": "chrome"
}
```

Or environment variables:

```bash
export FCRAWL_YT_COOKIES_FROM_BROWSER=chrome
export FCRAWL_YT_COOKIES_FILE="$HOME/Downloads/youtube-cookies.txt"
```

### JSON Output (for scripts)

```bash
# Get JSON for processing
fcrawl scrape https://example.com --json

# Pipe to jq
fcrawl scrape https://api.example.com --json | jq '.metadata.title'

# Get only links
fcrawl scrape https://example.com -f links --json
```

### Crawling Websites

```bash
# Crawl with limits
fcrawl crawl https://blog.com --limit 10

# Crawl with depth control
fcrawl crawl https://docs.site.com --depth 2

# Exclude paths
fcrawl crawl https://site.com --exclude-paths "/admin/*" "/private/*"

# Save all pages to JSON
fcrawl crawl https://blog.com --limit 20 -o blog-posts.json
```

### Mapping Sites

```bash
# Discover all URLs
fcrawl map https://docs.site.com

# Search for specific content
fcrawl map https://docs.site.com --search "api"

# Limit results
fcrawl map https://site.com --limit 100
```

### Searching the Web

```bash
# Fast Google search (Serper API)
fcrawl search "python tutorials"

# More results
fcrawl search "AI news" --limit 30

# Locale and region
fcrawl search "restaurants" -L zh-TW

# Location bias
fcrawl search "coffee shops" --location "New York"

# Multi-engine browser search (Camoufox)
fcrawl csearch "python tutorials"

# Save as JSON
fcrawl search "machine learning papers" --json -o results.json
```

> **Migrating from the old `search` command?** The previous Firecrawl-backed search
> supported `--sources`, `--category`, `--tbs` (time filter), `--scrape`, `--no-links`,
> and `--format`. These flags have been removed. Replacements:
>
> - **`--sources news`** / **`--category github`** — Use `site:` operators in the query
>   (e.g., `fcrawl search "site:github.com web scraping"`)
> - **`--tbs qdr:d`** — Serper doesn't expose a time filter; append time words to the
>   query (e.g., `"AI news today"`) or use `csearch` which searches live SERPs.
> - **`--scrape`** — Pipe search results into `fcrawl scrape`:
>   `fcrawl search "query" --no-pretty | xargs -I{} fcrawl scrape {}`

### Extract Structured Data

```bash
# Extract specific fields
fcrawl extract https://store.com --fields "price,title,description"

# Use custom prompt
fcrawl extract https://store.com --prompt "Extract product details"

# Multiple URLs
fcrawl extract url1 url2 url3 --fields "price,title"
```

### Quick Commands

```bash
# Quick scrape (no options, just markdown to stdout)
fcrawl quick https://example.com

# Quick search
fcrawl search "your query"

# View configuration
fcrawl config
```

## Advanced Usage

### Piping and Unix Tools

```bash
# Find all Python documentation links
fcrawl scrape https://docs.python.org -f links --no-pretty | grep "\.html"

# Monitor price changes
fcrawl scrape https://store.com/product --json | jq '.price' > today.txt
diff yesterday.txt today.txt

# Build documentation archive
for url in $(fcrawl map https://docs.site.com --no-pretty); do
  fcrawl scrape "$url" -o "docs/$(echo $url | md5).md"
done
```

### Fish Shell Integration

Add to your `~/.config/fish/config.fish`:
```fish
# fcrawl abbreviation
abbr --add fc fcrawl
abbr --add fcs 'fcrawl scrape'
abbr --add fcm 'fcrawl map'
abbr --add fcc 'fcrawl crawl'
```

## Examples

### Daily News Digest
```bash
fcrawl crawl https://news.ycombinator.com --limit 30 -o hn-today.json
```

### Research Assistant
```bash
# Find latest AI research papers
fcrawl search "large language models arxiv" --limit 20 -o ai-research.json

# Find GitHub repos
fcrawl search "site:github.com web scraping python" --limit 10
```

### News Aggregator
```bash
# Get today's tech news
fcrawl search "AI breakthrough" --limit 20 -o news-today.json
```

### Documentation Lookup
```bash
fcrawl scrape https://docs.python.org/3/library/asyncio.html --copy
```

### Site Analysis
```bash
fcrawl map https://competitor.com --search "pricing" -o competitor-pricing-pages.json
```

### Content Monitoring
```bash
# Check for updates
fcrawl scrape https://blog.com/latest -o latest.md
git diff latest.md  # See what changed
```

## Tips

- Use `--json` for scripting and automation
- Use `--no-pretty` when piping to other tools
- Use `-o -` to explicitly output to stdout
- Combine with `jq`, `grep`, `awk` for powerful workflows
- Set defaults in `~/.fcrawlrc` to save typing

## Troubleshooting

### Connection Refused
Make sure Firecrawl is running:
```bash
docker-compose up -d  # In firecrawl directory
```

### No Output
Check if the site has content:
```bash
fcrawl scrape URL --json  # See full response
```

### Rate Limiting
Adjust crawl speed:
```bash
fcrawl crawl URL --poll-interval 5 --timeout 600
```

## License

MIT

## Contributing

Pull requests welcome! The code is in `firecrawl-workspace/scripts/fcrawl/`
