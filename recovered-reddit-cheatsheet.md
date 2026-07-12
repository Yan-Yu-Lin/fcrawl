# Reddit Command Cheatsheet

This file summarizes the Reddit tooling in `fcrawl` and what output formats it supports.

## Command Matrix

| Tool / Command | What it does | Input style | Output formats | Save to file | Cache flags | Notes |
|---|---|---|---|---|---|---|
| `fcrawl reddit post` | Fetch one post + comment thread | URL or post ID (example: `1r6y419`) | Pretty terminal (default), plain text (`--no-pretty`), JSON (`--json`) | `-o file.json` | `--no-cache`, `--cache-only` | Supports `--no-comments`, `--limit`, `--depth`, `--sort` |
| `fcrawl reddit search` | Search Reddit posts | Query text | Pretty, plain, JSON | `-o` | `--no-cache`, `--cache-only` | Supports `--subreddit`, `--user`, `--after` cursor |
| `fcrawl reddit subreddit` | Browse subreddit feed or about info | `python` or `r/python` | Pretty, plain, JSON | `-o` | `--no-cache`, `--cache-only` | Supports `--sort`, `--time`, `--after`, `--about` |
| `fcrawl reddit user` | Fetch profile and activity | `spez` or `u/spez` | Pretty, plain, JSON | `-o` | `--no-cache`, `--cache-only` | Supports `--posts-only`, `--comments-only`, `--after` |
| `fcrawl scrape` (generic web) | Scrape webpage content directly | URL | `markdown` (default), `html`, `links`, JSON wrapper (`--json`) | `-o` | `--no-cache`, `--cache-only` | Useful for raw page extraction instead of Reddit-structured output |

## For a Single Reddit Post URL

Given a URL like:

`https://www.reddit.com/r/ClaudeCode/comments/1r6y419/is_claude_code_addictive/`

you can fetch it in multiple ways:

| Command | Result |
|---|---|
| `fcrawl reddit post "<url>"` | Post + comments (human readable) |
| `fcrawl reddit post 1r6y419` | Same, using post ID |
| `fcrawl reddit post 1r6y419 --json` | Structured JSON output |
| `fcrawl reddit post 1r6y419 --no-comments --json` | Post-only JSON (no comments payload) |
| `fcrawl reddit post "<url>" --no-pretty` | Plain text output (good for pipes) |
| `fcrawl scrape "<url>"` | Markdown page content |
| `fcrawl scrape "<url>" -f html` | Raw HTML |
| `fcrawl scrape "<url>" -f links` | Extracted links list |
| `fcrawl scrape "<url>" --json` | JSON-wrapped scrape output |
