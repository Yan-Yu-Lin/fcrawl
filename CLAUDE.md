# fcrawl — notes for agents working on this repo

fcrawl is a Python CLI (Click + Rich, managed with uv) that gives AI coding agents web access: Google search, page scraping, Reddit, X, YouTube, Dcard and the Firecrawl Developer Index. Its main users are Claude Code agents, so output formats are an interface. User-facing docs: `README.md`, `docs/commands.md`, `docs/direct-mode.md`.

## Two modes

- **Gateway mode (normal).** `~/.fcrawlrc` has `api_url: https://fcrawl.arthurlin.dev` and a `gateway_token` (`fgw_…`). The private server **fcrawl-gateway** (`~/20-29-Development/21-Active-Projects/fcrawl-gateway`, repo `Yan-Yu-Lin/fcrawl-gateway`) holds every real key and does the Firecrawl key pool, quotas, cache, Serper, X and goto-ticket resolution. The client only sends the token. Arthur, his dad and a friend run this way.
- **Direct mode.** No token: Firecrawl/Serper/X keys in `.fcrawlrc`, with a local key pool (`utils/keypool.py`). Keep it working, but gateway mode comes first.

## Branch, install, release

- Users install from GitHub **`main`**: `uv tool install --force "git+https://github.com/Yan-Yu-Lin/fcrawl.git"`, then `fcrawl setup claude-code`. Whatever is pushed to `main` reaches Arthur's family on their next upgrade. Work on a side branch, test, then fast-forward `main`.
- `feat/gateway-mode` is kept as a mirror of `main` (older installs point at it): when `main` moves, push the same commit there too (`git push origin main:feat/gateway-mode`).
- Arthur's Mac uses the same git install (not editable). To try a change for real: push + reinstall, or `uv run fcrawl …` inside the repo.
- The version lives only in `pyproject.toml` (`fcrawl --version` reads the package metadata).

## Layout

```
src/fcrawl/
  cli.py                 registers every command; `config` lives here
  commands/              one module per command: scrape, search, resolve, map, reddit, x, dcard, dev, papers,
                         yt_transcript, yt_channel, yt_search, transcribe, cache_cmd, setup, auth
    _x_gateway.py        X through the gateway (/gw/x/gql); _x_twitterapi.py, _x_diagnose.py other X backends
  utils/config.py        config loading, is_gateway(), check_gateway_url(), get_firecrawl_client(), firecrawl_call()
  utils/cache.py         file cache in the temp dir + validate_result() (blocked/empty pages never count as success)
  utils/keypool.py       direct-mode Firecrawl key pool, shared across processes
  utils/goto.py          Google goto tickets (g:xxxxxx ids)
  utils/platform.py      temp dir (/tmp, %TEMP% on Windows), OS differences
  utils/output.py        output, UTF-8 stdio, Progress (stderr, off when not a TTY)
  utils/transcriber.py   optional SenseVoice ASR (extra `asr`, lazy import)
  utils/institution.py   institution (library) proxy: presets, cookie store (~/.fcrawl/institution, 0600),
                         rate limits (file lock, configurable within a hard ceiling), fetch through the
                         proxy; `auth`, `papers fetch` and the `papers pdf` fall-through sit on it
  utils/browser_login.py `auth login`'s own browser window: system Chromium + temp profile, cookies over CDP
  vendors/twitterapi_io/ twitterapi.io client + mapper
  assets/claude/         SOURCE OF TRUTH for the Claude Code skill (skills/fcrawl/SKILL.md) and hook
                         (hooks/fcrawl_gate.py) that `fcrawl setup claude-code` installs
tests/                   pytest, no network
```

Dependencies worth knowing: `firecrawl-py` and `twscrape` are pinned to Arthur's forks in `[tool.uv.sources]`. Change dependencies with `uv add` / `uv remove`, never by hand.

## Tests and lint

```bash
uv run --with pytest python -m pytest -q     # 342 tests, all offline
uvx ruff check src tests                     # config in pyproject: pyflakes + syntax only
```

Smoke tests against the live gateway cost real credits (search 1 Serper credit, scrape ~1 Firecrawl credit). Keep them few, and never loop them.

## Rules

- Never print, log or commit keys or tokens. `fcrawl config` masks them, and tests use fake values.
- The gateway token goes only over https, or plain http to loopback/LAN/tailnet hosts (`check_gateway_url`). Don't weaken that.
- A quota error from the gateway is 429 + Retry-After, never 402: in direct mode the client permanently disables a key on 402.
- Results go to stdout and progress/notes to stderr. `RESULT path=… status=…` lines (scrape `--save-temp`, dcard `-o`) are parsed by agents, so don't change their shape casually.
- Output must stay UTF-8-safe on Windows (cp950 consoles): `force_utf8_stdio()` runs first in `cli.py`; open files with `encoding="utf-8"`.
- When you edit the skill or hook, edit `src/fcrawl/assets/claude/...`, then run `fcrawl setup claude-code` to refresh `~/.claude`. Don't edit the installed copy alone.
- A new gateway endpoint also has to be added to the gateway's nginx whitelist (`deploy/nginx-fcrawl.conf` in the gateway repo), or it returns 404 in production.
- Use `trash`, not `rm`, for files outside the repo.
- Institution proxy cookies (`utils/institution.py`) stay on the machine: never send them to the gateway or Firecrawl, never log or print them (nor browser URLs from the login window: one carries the student id and a token), and send them over https to the proxy host only.
- A username/password login for `fcrawl auth` is allowed only as something the user turns on themselves (off by default): the password lives in the OS keyring only (macOS Keychain, Windows Credential Manager, Linux Secret Service), never in `.fcrawlrc` or any other file, is never printed or logged, and goes only to the school's login endpoint. No captcha solving.

## Known gaps

- `dev` calls Firecrawl Cloud directly (not the gateway): free per-IP tier unless `api_key` is set.
- `papers` Research Index calls (search/show/read/related) and the `papers pdf` OA lookups (OpenAlex, Semantic Scholar) go directly from the client, keyless; only Scholar goes through the gateway (`/gw/search` type scholar).
- `scrape -f extract` is a leftover format option, untested against the gateway.
- With no `.fcrawlrc`, `api_url` defaults to `http://localhost:3002` (self-hosted Firecrawl), and a newcomer sees "connection refused".
- Institution proxy, not done yet: publisher hosts that contain a `-` (EZproxy writes it `--`; unknown whether Hyweb does) break hostname rewriting/`_unproxy`; cookie `path` is ignored when sending; `auth status` probes every time (no cache); the `prefix` style is untested against a real library. Arc cannot host the login window (no DevTools port); the snap-Chromium profile place (`~/snap/<name>/common`) is untested on Linux. The live ntut checks are done by an NTUT student, not here: nobody on this side has a login.
