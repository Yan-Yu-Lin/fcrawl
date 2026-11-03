# fcrawl command reference

Every command prints its full option list with `--help`. This page covers what each command is for and the options that matter. Most commands also take `-o FILE` (save), `--json`, `--copy` (clipboard), `--pretty/--no-pretty` (plain output is automatic when piped), `--no-cache` and `--cache-only`.

Costs are given in gateway credits. Commands with no cost listed are free: they use yt-dlp or public JSON endpoints.

## scrape — a page as markdown

```bash
fcrawl scrape URL --save-temp            # save to a temp file, print `RESULT path=…`
fcrawl scrape URL --article --no-links   # article text only (not for API docs: too aggressive)
fcrawl scrape URL --raw --wait 5000      # no filtering, let JavaScript render first
fcrawl scrape URL -f links               # all links (from the markdown)
fcrawl scrape URL -i ".main" -e ".ads"   # CSS include / exclude
fcrawl scrape g:ab12cd --save-temp       # a [goto] result from `fcrawl search`
```

| Option | Meaning |
|---|---|
| `-f, --format` | `markdown` (default), `html`, `links`, `screenshot`; repeatable |
| `--save-temp` | write to `/tmp/fcrawl-saved/` (Windows: `%TEMP%\fcrawl-saved\`) and print only the path |
| `--article` / `--raw` | aggressive filtering / no filtering |
| `--no-links` | `[text](url)` → `text` |
| `--wait MS`, `--timeout S` | wait before capture; hard timeout (default 12 s) |
| `--screenshot-full` | full-page screenshot |
| `--no-jina` | never fall back to Jina Reader |

About 1 Firecrawl credit per page (PDFs are charged per page). Firecrawl goes first; Jina Reader is tried only if Firecrawl fails or returns a blocked page. Both results must pass the same check: challenge pages, HTTP errors and empty pages are failures (`status=blocked`) and are never cached.

## search — Google via Serper

```bash
fcrawl search "query"
fcrawl search "query" -l 20              # each extra page = 1 more credit
fcrawl search "query" -L zh-TW --location "Taipei, Taiwan"
fcrawl search "query" --since 2w         # or --tbs qdr:w
fcrawl search "query" --news
fcrawl search "site:github.com query"
fcrawl search "query" --urls-only
```

Chinese queries automatically get Taiwan results (`gl=tw`, `hl=zh-tw`). Gateway-only options:

- `--engine firecrawl`: Firecrawl's search instead (2 Firecrawl credits per 10 results, real URLs).
- `--single` / `--batches N`: control how many identical Serper searches are merged.
- `--resolve`: resolve every `[goto]` ticket after the search.
- `--debug`: show provider, timing and batch mode.

**Goto tickets.** Google sometimes replaces result links with signed `google.com/goto` tickets. fcrawl prints these as `[goto] <breadcrumb>` with a `→ fcrawl scrape g:xxxxxx` line. Scrape the `g:` id directly: fcrawl resolves it while scraping.

## resolve — goto ticket to real URL

```bash
fcrawl resolve g:ab12cd g:ef34gh [--wait] [--json]
```

Only needed when you want the URL without reading the page. Gateway mode only.

## reddit — public Reddit JSON

```bash
fcrawl reddit post URL_OR_ID [--sort top] [-l 0] [-d 5] [--no-comments] [--json]
fcrawl reddit search "query" [-s subreddit] [-u author] [--sort top -t month] [-l 50]
fcrawl reddit subreddit python [--sort top -t week] [--about]
fcrawl reddit user NAME [--posts-only | --comments-only] [--about]
```

`-l 0` on `post` means all top-level comments. `--after CURSOR` pages through listings. If Reddit blocks the JSON endpoints, fcrawl falls back to the Arctic Shift archive and to Google (`site:reddit.com`) for search.

## x — X / Twitter

```bash
fcrawl x search "query" [--sort top|latest|photos|videos] [-l 50]
fcrawl x tweet URL_OR_ID [--thread] [--with-replies] [--reply-limit 30]
fcrawl x article URL_OR_ID [--raw]       # long-form X articles
fcrawl x user HANDLE
fcrawl x tweets HANDLE [-l 50]
fcrawl x diagnose                        # preflight checks, structured report
```

`--provider` chooses the backend: `gateway` (default in gateway mode), `twscrape` (your own X accounts, the default otherwise), or `twitterapi` (twitterapi.io key). `x accounts add-tokens --ct0 … --auth …`, `add`, `login` and `reset` manage your own accounts in `~/.config/fcrawl/x_accounts.db`. That is direct mode only.

## yt-transcript, yt-channel, yt-search — YouTube

```bash
fcrawl yt-transcript URL [-l zh-Hant] [--list-langs]
fcrawl yt-transcript URL --cookies-from-browser chrome   # when YouTube throttles (429)
fcrawl yt-channel "@handle" [-n 50] [-s views] [-q keyword] [-t shorts] [--with-dates] [--ids-only]
fcrawl yt-search "query" [-n 20] [-s date] [--ids-only]
```

Transcript language order: `-l` choice, then the video's own language, then English, then the first available. Cookies can also be set in `~/.fcrawlrc` (`yt_cookies_from_browser`, `yt_cookies_file`) or with `FCRAWL_YT_COOKIES_*` env vars. By default fcrawl retries with cookies when YouTube blocks (`--no-cookies-on-fail` turns that off).

## dcard — Dcard (Taiwan forum)

Dcard pages sit behind Cloudflare, so fcrawl reads Dcard's own JSON API through Firecrawl.

```bash
fcrawl dcard search "query" [-f forum] [--sort relevance|latest|trending|interaction] [--since 90] [-l 0]
fcrawl dcard topic NAME [--hot]
fcrawl dcard forum ALIAS [--popular] [--info]
fcrawl dcard forums [-s keyword] [--school]
fcrawl dcard post ID_OR_URL [ID ...] [--sort oldest|newest|likes] [--replies N|auto|auto:N] [-l 0] [-o dir/]
```

Costs: search/topic 1 credit per 30 posts, forum 1 per 100 posts, a post 1 credit plus 1 per 30 comments. List commands share `--compact` (one line per post), `--min-likes N` and `--by-likes`. Every command prints `credits: N` on stderr at the end. `--sort interaction` finds the long, well-liked experience posts. `--replies auto` expands the most-liked reply threads.

## dev — Firecrawl Developer Index

```bash
fcrawl dev "error message or question"
fcrawl dev "error" -t issue -t pull_request    # types: doc, issue, pull_request, readme
fcrawl dev "retry on 429" -r owner/repo -p 3   # one repo, 3 passages per result
fcrawl dev "config" --source DOC_SOURCE_ID
```

Searches GitHub issues, merged PRs, READMEs and curated docs, and returns the matching passage. Repo filters (`--language`, `--topic`, `--license`, `--min-stars`, `--max-stars`) drop doc results. `dev` always calls Firecrawl Cloud directly, not the gateway. It works without a key at a small daily per-IP limit, and with a Firecrawl `api_key` it uses that key (2 credits per 10 results).

## papers — academic papers (Google Scholar + Firecrawl Research Index)

```bash
fcrawl papers "query"                              # both engines, merged and deduped (= papers search)
fcrawl papers "query" --engine scholar|index|all   # one engine only
fcrawl papers "query" --since 2024 [--until 2025]  # year or YYYY-MM-DD
fcrawl papers "query" --authors Vaswani --category cs.LG   # Research Index filters
fcrawl papers "query" --sort citations|date -l 20 --abstracts --json
fcrawl papers show ID                              # metadata + full abstract (OpenAlex for DOIs the index lacks)
fcrawl papers read ID -q "question" [-k 4]         # full-text passages that answer the question
fcrawl papers related ID [--mode similar|citers|references] [--intent "..."]
fcrawl papers pdf ID [--no-scholar]                # legal free copy, then: fcrawl scrape <url> --save-temp
fcrawl papers fetch ID_OR_URL [-o file.pdf]        # the PDF through your own library proxy (see `auth`)
```

IDs: `arxiv:1706.03762`, `doi:10.1038/...`, `pmid:N`, `pmcid:PMCN`, bare arXiv ids/DOIs, or arxiv.org / doi.org / PubMed / PMC URLs.

- **scholar** is Google Scholar through Serper (gateway: `/gw/search` with `type: "scholar"`; direct mode: `serper_api_key`). It covers every field including IEEE/ACM and gives the year, citation count and often a PDF link. Cost: 1 Serper credit per 10 results, shown as `credits:` on stderr. Google Scholar returns at most 10 per page, and a year filter can return fewer.
- **index** is Firecrawl's Research Index: about 43M abstracts from PubMed, bioRxiv, medRxiv and arXiv. It is free and keyless and called directly (like `dev`), with a Firecrawl `api_key` only raising the rate limit. It gives full abstracts and powers `read`, `related` and `show`. It is weak on engineering venues (IEEE circuits papers are mostly missing), and full-text passages exist mainly for arXiv papers.
- The default `all` runs both in parallel and interleaves them rank by rank. The same paper (same DOI, arXiv or PubMed id, or the same title of 4+ words from both engines) is shown once, with both engines' facts. If one engine fails, the other's results still print, with a warning.
- `--sort citations|date` re-sorts what was found; it does not search for the most-cited papers overall.
- `pdf` only looks at legal open-access sources: arXiv, OpenAlex OA locations (publisher OA pages, PMC and other repositories), Semantic Scholar's open PDF (keyless, often rate limited), and finally the paper's Scholar PDF links (1 Serper credit, skip with `--no-scholar`). Those last links are filtered by host: repositories, OA publishers and university (`.edu`, `.ac.*`) pages are shown, ResearchGate/Academia uploads are shown with a warning, and unknown hosts are hidden. When nothing is found it says so and gives the DOI link. There is no shadow-library support and none is planned. A publisher page behind a login wall scrapes as a normal page; read it and you will see it is abstract only.

- `fetch` downloads the PDF of a paywalled paper through the user's own library proxy (`auth` below) to a local file: `RESULT path=… provider=institution:<preset> url=… status=ok bytes=N`. It takes a DOI, a publisher URL or a DOI link; a DOI is first resolved at doi.org itself (public, no cookies) and the publisher page then goes through the proxy. arXiv and PMC ids are free and fetched directly (`provider=direct`, not counted). The landing page's `citation_pdf_url` is used when present; otherwise the known patterns of IEEE Xplore, Springer, ACM, Wiley and Taylor & Francis. Verified live through the ntut proxy: IEEE Xplore and Springer. ScienceDirect hands out PDFs only after a JavaScript challenge that no HTTP client passes: `fetch` says so and prints the proxied article URL to open in a browser. `fcrawl auth status` must be OK first; `papers pdf` mentions `fetch` only while the feature is on.

## auth — your library's subscription (institution proxy; off by default)

```bash
fcrawl auth presets                          # proxies fcrawl knows (built-in: ntut)
fcrawl auth enable ntut                      # turn it on; written to .fcrawlrc as institution.{enabled,preset}
fcrawl auth login                            # opens the library login page in your browser; log in there yourself
fcrawl auth login --from-browser firefox     # then hand the session cookies to fcrawl (yt-dlp's cookie reader)
fcrawl auth login --cookie-file cookies.txt  # or a Netscape cookies.txt exported after logging in
fcrawl auth login --paste                    # or the Cookie: header value from the dev tools (hidden input)
fcrawl auth status [--no-probe] [--json]     # cookie names (never values), expiry, one probe request, today's usage
fcrawl auth logout | disable
```

- fcrawl never asks for the password and never solves a captcha: the login happens in the user's own browser, fcrawl only takes over the session cookies that login left behind. On Windows, Chrome and Edge encrypt their cookies so that only they can read them; Firefox works, `--cookie-file` and `--paste` always work.
- Cookies live in `~/.fcrawl/institution/<preset>.cookies.json` (owner-only; `FCRAWL_STATE_DIR` moves the folder) and are sent to the proxy host and its subdomains only, never to a publisher directly, never to the gateway or Firecrawl.
- Built-in limits, kept in `<preset>.usage.json`: 20 papers a day, 10 per publisher a day, at least 30 s between two papers (fcrawl waits). They exist because bulk downloading is what gets a whole school cut off by a publisher.
- A preset is one library proxy: `proxy_host`, `style` (`hostname`: `ieeexplore.ieee.org` → `ieeexplore-ieee-org.<proxy_host>`, used by EZproxy "proxy by hostname" and Hyweb; `prefix`: `https://<proxy_host>/login?url=<url>`, classic EZproxy), `login_url` (with `{url}`), `login_marker` (where an unauthenticated request is redirected) and `probe_url`. Add your own under `institution.presets.<id>` in `.fcrawlrc` with those fields; `fcrawl auth presets` lists it.
- Session gone (the proxy redirects to its login page) → `fcrawl auth login` again. For ntut the session cookie is `HYSID` on `.search.lib.ntut.edu.tw` (a browser session cookie; the server side was seen with a 2 h lifetime). Cookies the proxy sets on the way — one set per publisher host, and the ones it rewrites onto a parent of its own domain — are kept in the store too, so the next paper skips the handshake.
- Browsers: `--from-browser` reads the browser's cookie database with yt-dlp's extractor. It needs the database to be readable: Firefox works while running; Chrome and Edge on Windows encrypt with a key only they hold, and a Chromium that is running (Electron apps included) keeps the file locked. Then export a cookies.txt or use `--paste`.

## map — discover a site's URLs

```bash
fcrawl map https://docs.example.com [--search api] [--limit 100] [--include-subdomains]
```

## cache

```bash
fcrawl cache stats          # files per command / provider, how many are junk
fcrawl cache clean [--yes]  # list (and with --yes, trash) junk scrape entries
fcrawl cache clear [scrape|search|…]
```

## config

`fcrawl config` prints the active configuration with keys and tokens masked.

## setup claude-code

```bash
fcrawl setup claude-code              # install / update skill + hook + settings + CLAUDE.md section
fcrawl setup claude-code --dry-run
fcrawl setup claude-code --doctor     # check an existing install, run the hook once
fcrawl setup claude-code --uninstall
```

Installs `skills/fcrawl/SKILL.md` and `hooks/fcrawl_gate.py` into `~/.claude` (or `$CLAUDE_CONFIG_DIR`). The hook blocks the built-in WebSearch/WebFetch and blocks `fcrawl` calls from an agent that has not loaded the skill. Run it again after every upgrade.

## transcribe

Local speech-to-text with SenseVoice (Alibaba FunASR). Needs the optional `asr` extra, which pulls in PyTorch, FunASR, pydub and OpenCC (several GB):

```bash
uv tool install --force "fcrawl[asr] @ git+https://github.com/Yan-Yu-Lin/fcrawl.git"
fcrawl transcribe recording.mp3 [-m sensevoice|paraformer|fun-asr-nano] [-f txt|srt|vtt|json] [--simplified]
```

With the extra installed, `yt-transcript` also falls back to local transcription for videos without subtitles (`-t` forces it, `--no-transcribe` disables it). Chinese output is Traditional unless you pass `--simplified`. Runs on Apple Silicon (MPS), CUDA or CPU.
