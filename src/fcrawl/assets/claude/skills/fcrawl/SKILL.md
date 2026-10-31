---
name: fcrawl
description: How to do ANY web task on this machine — web search, reading a page or API docs, YouTube transcripts, Reddit, X/Twitter, Dcard — with the `fcrawl` CLI. Load this before touching the web; built-in WebSearch/WebFetch are blocked by a hook until this skill has been read. Covers the hard rules (search results stay in Bash output, scrape with --save-temp, never --no-links on docs), every subcommand, and how to navigate API docs (llms.txt → .md pages).
---

# fcrawl — all web access goes through this CLI

`fcrawl` is Arthur's own CLI (Firecrawl + Serper + yt-dlp + Reddit + X + Dcard) built for agents. The built-in WebSearch/WebFetch tools summarise pages through a sub-model; fcrawl gives you the real content. A hook denies WebSearch/WebFetch until this skill has been loaded in the current agent; after that they are allowed if you still need them, but you almost never will.

## Hard rules (these survive compaction — keep them in mind)

1. **Never WebSearch / WebFetch / Firecrawl MCP.** Use `fcrawl` via Bash.
2. **`fcrawl search` results stay in the Bash output.** Do not redirect them to files, even when running several searches in parallel — all titles/URLs/snippets must land in context before you pick what to read.
3. **`fcrawl scrape` uses `--save-temp`**, then Read the file it names. Page bodies are large.
4. **Never `--no-links` on docs, API references, or `llms.txt`.** Links are the map. `--no-links` only on blog articles.
5. **Never `--article` on docs.** It strips too much. Plain scrape for docs.
6. **Read scraped docs pages fully** — at least 500 lines; chunk if longer.
7. **Parallel network calls = one Bash call** with `( … ) &` per command and `wait` at the end. Not one Bash call per request.
8. **Reddit → `fcrawl reddit`, Dcard → `fcrawl dcard`, X → `fcrawl x`, YouTube → `fcrawl yt-*`.** Scraping those sites' pages directly fails or wastes credits.
9. Save outputs with `-o file` or `> file`, not the Write tool.
10. Don't `cat` files in Bash; use the Read tool.

## Web search

`search` is Serper-backed (Google). Default for any lookup.

```bash
fcrawl search "query"                             # default
fcrawl search "query" -l 30                       # more results
fcrawl search "query" -L zh-TW                    # locale
fcrawl search "query" --location "Taipei, Taiwan" # location bias
fcrawl search "site:github.com web scraping"      # domain targeting
fcrawl csearch "query"                            # browser multi-engine fallback (slow)
fcrawl gsearch "query"                            # browser Google fallback
```

**`[goto]` results are normal, not broken.** Google wraps results after the top few in opaque
tickets. They print as `[goto] https://host › path` (the real site's breadcrumb; title and snippet are
real) with a `ticket: https://www.google.com/goto?url=…` line under them. Choose by title, snippet
and breadcrumb as usual, then:

- To read one: `fcrawl scrape <ticket> --save-temp` — the gateway swaps in the real URL (header line
  `Resolved: ticket → real`), waiting ~20 s only in rare cases.
- Need the real URLs first (e.g. to judge the source): `fcrawl search "query" --resolve`, or later
  `fcrawl resolve <ticket…> --wait`.
- A `[goto]` line without a breadcrumb (only "10+ comments · 1 year ago") is a social post
  (FB/IG/Threads/Reddit); `--resolve` tells you which.
- One search is two merged Google batches by default (slightly more results than before);
  `--single` makes it one. `goto_unresolved` errors: wait 20 s and retry, or `fcrawl resolve … --wait`.

## Code / docs search (Developer Index)

For error messages, library behaviour, known bugs: `fcrawl dev` searches GitHub issues, PRs, READMEs, curated docs and returns the matching passage.

```bash
fcrawl dev "error message or question"
fcrawl dev "error message" -t issue -t pull_request   # where was it reported/fixed
fcrawl dev "how to configure X" -t doc
fcrawl dev "retry on 429" -r owner/repo -p 3
```

Use `dev` first when debugging a library; fall back to `search`. Results stay in Bash output like `search`.

## Scraping pages

```bash
fcrawl scrape URL --save-temp                 # then Read the RESULT path
fcrawl scrape URL --article --no-links        # blog/article only
fcrawl scrape URL -f links                    # all links on a page
fcrawl scrape URL --raw --wait 5000 -f links  # JS-heavy nav, link discovery
fcrawl scrape URL --wait 3000                 # social/dynamic pages
fcrawl map URL                                # discover a site's URLs
```

| Content | Flags | Why |
|---|---|---|
| Blog/article | `--article --no-links` | declutter |
| Docs / API reference | plain, or two-pass | need links |
| Link extraction | `--raw --wait 5000 -f links` | let JS render |
| Social | `--wait 2000`–`3000` | dynamic |

Two-pass for docs when you want clean text *and* links: `scrape URL --no-links` for the body, `scrape URL -f links` for navigation. CSS filters: `-i ".content"`, `-e ".sidebar"`.

Parallel pages:

```bash
(fcrawl scrape "URL1" --save-temp --no-cache) &
(fcrawl scrape "URL2" --save-temp --no-cache) &
wait
```

**Fallback/validation:** Firecrawl first through a pool of accounts (429s are handled in the pool — parallel is fine). Only on a real failure or a bot-challenge page does it fall back to Jina, and every result is validated: a Cloudflare "Just a moment…" page is an error, not content. Blocked → `RESULT … status=blocked`, nothing cached. `--no-jina` disables the fallback; `fcrawl cache clean --yes` trashes junk cache entries. Taiwan school/government legacy sites (`w3.*.edu.tw`) geo-block foreign IPs — `ERR_CONNECTION_CLOSED` there is not a bug; try the newer site or plain `curl` locally.


## Reading API docs (do this whenever you need a service's documentation)


When you need up-to-date documentation for any service, don't rely on training data — it's probably stale. Read the actual docs with fcrawl, the way a developer opens them in a browser.

### 0. Find the official docs domain first

Before guessing `*/llms.txt`, find the canonical docs home with a simple search:

```bash
fcrawl search "<product> documentation"
```

**Hard rule:** do NOT guess docs domains or `llms.txt` paths at the start. Identify the official domain, then probe `llms.txt` there. Use broad queries first — `site:` filters or `llms.txt` in the query early on can anchor you to the wrong domain.

### 1. Try `llms.txt` first

`llms.txt` is a convention (like `robots.txt`) where doc sites publish an LLM-friendly index of all their pages. Try the root and the `/docs/` path:

```bash
fcrawl scrape https://example.com/llms.txt
fcrawl scrape https://example.com/docs/llms.txt
```

Use `llms.txt`, NOT `llms-full.txt` — the full version dumps every page and floods context. You want the index: a structured list of pages and what they cover. From there, scrape individual pages.

**Never `--no-links` on `llms.txt`** — the whole point is the URL index; stripping link targets destroys the map.

**Never `--no-links` on any docs/API site** — links are how you discover related pages, parameters, types, examples. Only `--no-links` on blog articles.

**Read scraped doc pages fully** — at least 500 lines; if a page is 1000+ lines, read it in chunks but read all of it. Parameters, examples and edge cases are deep in the page.

### 2. No `llms.txt`? Discover the site map manually

Start from the docs home and extract links:

```bash
fcrawl scrape https://docs.example.com --raw -f links --wait 3000
```

That's the first layer (sidebar, main sections). JS-rendered nav and collapsed sections hide deeper links, so go one level deeper on section overview pages:

```bash
fcrawl scrape https://docs.example.com/api/overview --raw -f links --wait 3000
fcrawl scrape https://docs.example.com/guides --raw -f links --wait 3000
```

Two passes usually get 90% of the pages.

### 3. Two modes: exploring vs reading

**Exploring (building the map):** scrape normal URLs — you need nav, sidebar, breadcrumbs.

```bash
fcrawl scrape https://docs.example.com/api/messages --raw -f links --wait 3000
```

**Reading (consuming content):** append `.md` or `.mdx` to the URL. Many doc sites serve the full markdown that way, including all code tabs (Python / TS / Go / curl) that the HTML view shows one at a time.

```bash
fcrawl scrape https://docs.example.com/api/messages.md --no-links --save-temp
fcrawl scrape https://docs.example.com/api/messages.mdx --no-links --save-temp
```

Try the `.md` suffix even when `llms.txt` doesn't hint at it — it often works. **Don't combine `.md` URLs with `--raw -f links`** — the markdown view has no nav to extract.

### 4. When you can't find something

```bash
fcrawl search "site:docs.example.com streaming responses"
```

Targeted Google search within the docs domain, then scrape the hit.

## YouTube

```bash
fcrawl yt-transcript URL                  # transcript
fcrawl yt-transcript URL -l zh-Hant       # specific language
fcrawl yt-transcript URL --list-langs
fcrawl yt-channel "@handle"               # recent videos
fcrawl yt-channel "@handle" --sort views
fcrawl yt-channel "@handle" --search "topic"
fcrawl yt-channel "@handle" --type shorts
```

## Reddit (public JSON, no auth)

```bash
fcrawl reddit search "query" [-s subreddit] [-u author]
fcrawl reddit post URL_OR_ID [--no-comments] [--json]   # --json is best for agents
fcrawl reddit subreddit python [--about]
fcrawl reddit user spez [--comments-only]
```

Pagination: `--after` with the cursor from the previous JSON output.

## Dcard (Taiwanese forum, via Dcard's own JSON API)

```bash
fcrawl dcard search "query" --sort interaction            # best for high-quality experience posts
fcrawl dcard search "query" -f military --sort latest --since 90
fcrawl dcard forum military --compact --min-likes 5       # 100 posts/credit, one-line table, junk filtered
fcrawl dcard forum military --popular
fcrawl dcard topic 當兵 [--hot]
fcrawl dcard forums -s 軍                                  # find forum aliases
fcrawl dcard post URL_OR_ID --sort likes                  # full post + comments (B1/B2… floors, 原PO marked)
fcrawl dcard post ID --limit 0 --replies auto             # all comments, expand 3 most-liked reply threads
fcrawl dcard post ID1 ID2 ID3 -o posts/                   # several at once, one file each
```

Lists give titles + excerpts only; pick posts, then `dcard post`. `--compact` for big lists (300 posts ≈ 10k tokens). Each request = 1 Firecrawl credit; every command prints `credits: N (M cached)` on stderr.

## X / Twitter

```bash
fcrawl x search "query" [--sort latest]
fcrawl x tweet URL [--thread] [--thread --with-replies]   # --with-replies for discussions
fcrawl x user handle
fcrawl x tweets handle
```

## General

- `--no-cache` fresh fetch, `--cache-only` skip API, `--copy` clipboard, `fcrawl <cmd> --help` for flags.
- `fcrawl extract` is broken — scrape and process yourself.
- `fcrawl crawl` may miss links — `scrape URL --raw --wait 5000 -f links` first, then scrape each.
- `fcrawl cache stats | clean --yes | clear <bucket>`.

## Windows / PowerShell

- Prefer the **Bash tool** (Git Bash): every example in this file works there unchanged, and `fcrawl` is on PATH as `fcrawl.exe`. The PowerShell tool is gated the same way (load this skill first).
- In PowerShell: no `( … ) &` / `wait`, and PowerShell 5.1 has no `&&`/`||`. Run parallel searches as **separate Bash tool calls in one message**, or `Start-Job { fcrawl search "q" } ; Get-Job | Wait-Job | Receive-Job`. Chain with `;`.
- Paths: saved pages land under `$env:TEMP\fcrawl-saved\` (not `/tmp/fcrawl-saved`); the `RESULT path=…` line tells you where. Read them with the Read tool as usual.
- Output is UTF-8 regardless of the console code page; if you ever see `UnicodeEncodeError`, run `fcrawl setup claude-code --doctor`.

## Why fcrawl over the built-in tools
WebSearch/WebFetch pipe content through a summarisation sub-model before you see it — a book report, not the book. For API docs, parameter schemas, migration guides, the nuance is destroyed. fcrawl gives the real page: raw HTML, clean article, just links, CSS-filtered sections — your choice.
