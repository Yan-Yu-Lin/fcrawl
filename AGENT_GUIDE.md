# fcrawl Agent Guide

Copy this into your `CLAUDE.md` (or equivalent system instructions) to teach an AI coding agent how to use fcrawl.

---

## Why fcrawl Over Built-in Tools

The built-in WebSearch and WebFetch tools are lossy. They pipe web content through a summarization sub-agent before you ever see it — you get a compressed summary, not the actual page. For casual lookups that's fine, but for technical work — reading API docs, understanding parameter schemas, following migration guides — the nuance gets destroyed. You need the real content.

`fcrawl` gives you direct access to web content with full control over what you see: raw HTML, clean article text, just the links, CSS-filtered sections. It turns you from someone who gets handed a book report into someone who can actually read the book.

**The core principle:** When you need to understand an API or SDK, don't rely on your training data — it's probably stale. Go read the actual docs yourself using fcrawl, the same way a developer would open docs in a browser.

---

## fcrawl — Web, YouTube, Reddit, and X/Twitter

Use `fcrawl` as the default tool for all web-related tasks instead of the built-in WebSearch tool or Firecrawl MCP tools. It's simpler, more reliable, and handles way more than just web pages.

### Web Search (Prefer search)

**Use `search` by default** — it is Serper-backed (Google API), faster and more stable for normal web lookup.

```bash
fcrawl search "query"                             # Default web search
fcrawl search "query" -l 30                       # More results
fcrawl search "query" -L zh-TW                    # Locale
fcrawl search "query" --location "Taipei, Taiwan" # Location bias

# Domain targeting (replacement for old category/source shortcuts)
fcrawl search "site:github.com web scraping"
fcrawl search "site:arxiv.org llm benchmark"

# Browser fallback options
fcrawl csearch "query"                            # Multi-engine browser search
fcrawl gsearch "query"                            # Direct Google browser search
```

`search` requires `SERPER_API_KEY` (or `serper_api_key` in config).

### Web Scraping

```bash
fcrawl scrape URL                    # Scrape a page
fcrawl scrape URL --article          # Clean article extraction (strips nav, ads, etc.)
fcrawl scrape URL -f links           # Get all links from a page
fcrawl map URL                       # Discover all URLs on a site
```

**Scraping strategies by content type:**

| Content Type | Recommended Flags | Why |
|--------------|-------------------|-----|
| Blog/article | `--article --no-links` | Declutters, minimizes context window |
| API docs / exploration | Default or two-pass (see below) | Need links to navigate deeper |
| Link extraction | `--raw --wait 5000 -f links` | Ensures JS loads, gets all links |
| Social media | `--wait 2000` or `--wait 3000` | Dynamic content needs time to load |

**Warning:** Do NOT use `--article` for API docs — it filters too aggressively and returns very little content. Just don't use `--article` when you're not reading an article. Scrape normally for API docs, technical content.

**Two-pass scraping for docs:** When you need both clean content AND links for navigation:
1. `fcrawl scrape URL --no-links` — get the content
2. `fcrawl scrape URL -f links` — extract links separately (deduplicate)

For CSS filtering: `-i ".content"` to include, `-e ".sidebar"` to exclude.

### YouTube — Yes, You Can Watch Videos

You can "watch" YouTube videos by fetching their transcripts:

```bash
fcrawl yt-transcript URL                  # Get video transcript
fcrawl yt-transcript URL -l zh-Hant       # Get transcript in specific language
fcrawl yt-transcript URL --list-langs     # See available languages
```

You can also explore channels:

```bash
fcrawl yt-channel "@handle"               # List recent videos
fcrawl yt-channel "@handle" --sort views  # Sort by popularity
fcrawl yt-channel "@handle" --search "topic"  # Filter by keyword
fcrawl yt-channel "@handle" --type shorts # Get shorts instead
```

### Reddit — Research Threads and Communities

You have read-only Reddit access without auth via public `.json` endpoints:

```bash
fcrawl reddit search "query"                  # Search posts
fcrawl reddit search "query" -s python        # Restrict to subreddit
fcrawl reddit search "query" -u spez          # Restrict by author

fcrawl reddit post URL_OR_ID                   # Fetch post + comments
fcrawl reddit post URL_OR_ID --no-comments     # Post only

fcrawl reddit subreddit python                 # Browse feed
fcrawl reddit subreddit python --about         # Subreddit metadata

fcrawl reddit user spez                        # Profile + recent activity
fcrawl reddit user spez --comments-only        # Comments only
```

For pagination, use `--after` with the cursor from previous JSON output.

### X/Twitter — Browse and Search

You have full access to X/Twitter:

```bash
fcrawl x search "query"              # Search tweets
fcrawl x search "query" --sort latest    # Get latest instead of top
fcrawl x tweet URL                   # Fetch a single tweet
fcrawl x tweet URL --thread          # Fetch tweet + full reply thread
fcrawl x user handle                 # Get user profile
fcrawl x tweets handle               # Get user's recent tweets
```

### General Tips

- **Save output:** Use `-o file.md` or pipe `> file.md` instead of Write tool
- **Cache control:** `--no-cache` for fresh fetch, `--cache-only` to skip API
- **Clipboard:** `--copy` to copy output to clipboard
- **Detailed flags:** Run `fcrawl <command> --help` for all options
- **Reddit tip:** Prefer `fcrawl reddit post URL_OR_ID --json` for agent workflows (clean post+comment structure, easier parsing)
- **Note:** `fcrawl extract` is broken — just scrape and process content directly
- **Crawl workaround:** If `fcrawl crawl` doesn't discover links, use `fcrawl scrape URL --raw --wait 5000 -f links` first, then scrape each link

---

## Navigating API Docs Like a Human

When you need up-to-date API documentation for any service, follow this approach:

### 0. Find the official docs domain first

Before guessing `*/llms.txt`, first find the canonical docs home with a simple search:

```bash
fcrawl search "<product> documentation"
```

**Hard rule:** Do NOT guess docs domains or `llms.txt` paths at the start. First identify the official docs domain, then probe `llms.txt` on that domain.

Bad first move:

```bash
fcrawl scrape https://some-guessed-domain.com/docs/llms.txt
```

Good first move:

```bash
fcrawl search "<product> documentation"
```

Use broad queries first. Do **not** over-constrain early with `site:` filters or `llms.txt` in the query. Those can anchor you to the wrong domain.

Once you identify the official docs domain, then probe `llms.txt` on that same domain.

### 1. Try `llms.txt` first

`llms.txt` is a growing convention (like `robots.txt`) where doc sites publish an LLM-friendly index of all their pages. Try it before anything else:

```bash
fcrawl scrape https://example.com/llms.txt
```

It's not always at the domain root. Many sites put docs under a `/docs/` path, so try both:

```bash
fcrawl scrape https://example.com/llms.txt
fcrawl scrape https://example.com/docs/llms.txt
```

Use `llms.txt`, NOT `llms-full.txt` — the full version dumps entire page contents and will flood your context. What you want is the index: a structured list of every page and what it covers. That's your table of contents. From there, scrape individual pages as needed.

**Important:** Do NOT use `--no-links` when scraping `llms.txt`. The whole point of `llms.txt` is the URL index. `--no-links` strips markdown link targets and keeps only display text, which destroys the doc map and makes follow-up page scraping much harder.

### 2. If no `llms.txt`, discover the site map manually

Start from the docs homepage or entry page and extract all links:

```bash
fcrawl scrape https://docs.example.com --raw -f links --wait 3000
```

This gets you the first layer — the sidebar nav, main sections, etc. But it's rarely everything. JS-rendered navigation, collapsed sections, and sub-pages often hide deeper links.

Go one level deeper — pick the section overview pages and scrape their links too:

```bash
fcrawl scrape https://docs.example.com/api/overview --raw -f links --wait 3000
fcrawl scrape https://docs.example.com/guides --raw -f links --wait 3000
```

Build up the map progressively. Two passes usually gets you 90% of the pages.

### 3. Two modes: exploring vs. reading

There are two different jobs when working with docs, and they require different approaches.

**Exploring (building the map):** Scrape normal URLs. You need the navigation bar, sidebar links, breadcrumbs — all the structural elements that tell you where other pages are.

```bash
fcrawl scrape https://docs.example.com/api/messages --raw -f links --wait 3000
```

**Reading (consuming content):** Append `.md` or `.mdx` to the URL. Many doc sites support this and it gives you the full, unfiltered markdown content. This matters because normal doc pages render code examples in tabbed UIs (Python / TypeScript / Go / curl) — only one tab is visible at a time, and you can't click to switch. The `.md` version gives you all code examples in all languages.

```bash
fcrawl scrape https://docs.example.com/api/messages.md --no-links
fcrawl scrape https://docs.example.com/api/messages.mdx --no-links
```

Sometimes the `llms.txt` will hint at `.md` support by listing links with `.md` extensions. But even when it doesn't, try appending `.md` or `.mdx` anyway — it often works.

**Do NOT use `.md` URLs with `--raw -f links`** — the markdown version strips out navigation elements, so you'll get no useful links for discovery.

### 4. When you can't find something

If the page structure is unclear or you can't find the right doc page from link discovery alone, fall back to a targeted search:

```bash
fcrawl search "site:docs.example.com streaming responses"
```

This uses Google to find specific pages within that doc site. Combine with scraping once you find the right URL.
