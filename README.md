# fcrawl

fcrawl is a command-line tool that gives AI coding agents (Claude Code and similar) real access to the web: Google search, any page as clean markdown, Reddit threads, X/Twitter, YouTube transcripts, Dcard and GitHub issues/docs.

Why not the agent's built-in WebSearch/WebFetch? Those pass pages through a summariser first, so the agent gets a book report instead of the book. fcrawl hands over the real text, saves big pages to a file the agent reads, and never reports a blocked or empty page as a success.

## Quick start

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) and a gateway token from Arthur.

**1. Install**

```bash
uv tool install "git+https://github.com/Yan-Yu-Lin/fcrawl.git"
```

**2. Configure.** Create `~/.fcrawlrc` (on Windows: `C:\Users\<you>\.fcrawlrc`) with:

```json
{
  "api_url": "https://fcrawl.arthurlin.dev",
  "gateway_token": "fgw_..."
}
```

Keep the token private (`chmod 600 ~/.fcrawlrc`). fcrawl refuses to send it over plain `http://` except to home-LAN/tailnet addresses.

**3. Connect Claude Code** (installs the fcrawl skill and a hook that routes web access through fcrawl):

```bash
fcrawl setup claude-code
```

**Upgrade later:** `uv tool install --force "git+https://github.com/Yan-Yu-Lin/fcrawl.git" && fcrawl setup claude-code`.
`fcrawl setup claude-code --doctor` checks the installation.

## What you can do

**Read a page**

```bash
fcrawl scrape URL --save-temp          # full page saved to a temp file; prints its path
fcrawl scrape URL --article --no-links # just the article text
fcrawl scrape URL -f links             # every link on the page
```

**Search Google**

```bash
fcrawl search "query"                  # ~10 results (one Google page)
fcrawl search "query" -L zh-TW         # Taiwan / Traditional Chinese results
fcrawl search "query" --since 7d       # only recent results; --news for news
```

Sometimes Google hides a result's real address behind a ticket. fcrawl then shows `[goto] <site breadcrumb>` with a short id like `g:ab12cd`. To read it, run `fcrawl scrape g:ab12cd --save-temp` and fcrawl resolves the address itself.

**Reddit**

```bash
fcrawl reddit post URL --json          # post + comment tree
fcrawl reddit search "query" -s python # search (optionally in one subreddit)
```

**X / Twitter**

```bash
fcrawl x search "query" --sort latest
fcrawl x tweet URL --thread            # add --with-replies for the discussion
fcrawl x user handle                   # profile; `x tweets handle` for timeline
```

**YouTube**

```bash
fcrawl yt-transcript URL               # transcript (-l zh-Hant for a language)
fcrawl yt-channel "@handle" --sort views
fcrawl yt-search "query"
```

**Dcard** (new)

```bash
fcrawl dcard search "query" --sort interaction
fcrawl dcard post ID --sort likes      # full post + comments
fcrawl dcard forum military --compact  # latest posts in a forum
```

**Code and docs search** (new: GitHub issues, PRs, READMEs and curated docs, with the matching passage)

```bash
fcrawl dev "error message"
fcrawl dev "error message" -t issue -t pull_request
```

**Academic papers** (new: Google Scholar + Firecrawl's Research Index, merged)

```bash
fcrawl papers "query"                  # titles, year, citations, abstract, PDF link
fcrawl papers read arxiv:ID -q "..."   # the passages of one paper that answer a question
fcrawl papers pdf doi:...              # a legal free copy, if one exists
fcrawl papers fetch doi:...            # the PDF through your own library proxy (opt-in, see below)
```

More: `map`, `resolve`, `cache`, `config`, `x diagnose`, and every flag, in [docs/commands.md](docs/commands.md). Any command also takes `--help`.

## How it works

- **Gateway mode** (the setup above): fcrawl sends each request to the family gateway, which holds the real Firecrawl, Serper and X credentials, shares them fairly between people and caches results. Your machine holds only the token.
- **Checked results:** pages are fetched with Firecrawl, falling back to Jina Reader. Challenge pages, error pages and empty pages count as failures. They are reported as `status=blocked` and never cached.
- **Cache:** results are cached for 7 days in the temp folder (`/tmp/fcrawl-cache`, `%TEMP%` on Windows). `--no-cache` forces a fresh fetch.
- **Windows** works. Claude Code runs fcrawl through Git Bash, and output is UTF-8.

## Using your own keys (direct mode)

Without a gateway, fcrawl can call Firecrawl, Serper and X directly with your own keys, including a pool of several Firecrawl accounts. See [docs/direct-mode.md](docs/direct-mode.md).

## Optional: your library's subscription

Off by default. `fcrawl auth enable ntut`, then `fcrawl auth login`: it opens the library's login page, you log in there yourself (fcrawl never sees the password), and `fcrawl auth login --from-browser firefox` (or `--cookie-file`, `--paste`) hands the session cookies to fcrawl. From then on `fcrawl papers fetch doi:…` downloads paywalled PDFs through the proxy to a local file. The cookies stay on this machine, go to the proxy host only, and the command is rate limited (20 papers a day, 10 per publisher, 30 s apart). Built-in preset: `ntut` (Taipei Tech); other EZproxy/Hyweb libraries are a few lines in `.fcrawlrc` (`docs/commands.md`, `auth`).

## Optional: local transcription

`fcrawl transcribe FILE` and the `yt-transcript` fallback for videos without subtitles run SenseVoice speech recognition on your own machine. It is an optional extra (PyTorch + FunASR, several GB) and is not installed by default. Details in [docs/commands.md](docs/commands.md#transcribe).

## Development

```bash
git clone https://github.com/Yan-Yu-Lin/fcrawl.git && cd fcrawl
uv sync
uv run fcrawl --help
uv run --with pytest python -m pytest -q
```

Layout and project rules for contributors (human or AI) are in [CLAUDE.md](CLAUDE.md).

© Yan-Yu Lin. All rights reserved.
