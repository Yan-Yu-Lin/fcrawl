"""Google "goto" tickets on the client side.

Since 2026-10-30 Google wraps organic results 4+ in
`https://www.google.com/goto?url=CAES…` tickets. The gateway keeps them opaque
and resolves them when a page is actually fetched (`fcrawl scrape <ticket>`),
or on request (`fcrawl resolve`, `fcrawl search --resolve`). This module is
the small shared vocabulary: what a ticket looks like, how to show it, and the
`/gw/resolve` call.
"""

from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlsplit

import requests

from .config import gateway_headers, gateway_url, is_gateway, load_config

GOTO_HOST_RE = re.compile(r"^(www\.)?google\.(?:[a-z]{2,3}\.)?[a-z]{2,3}$")
RESOLVE_TIMEOUT = 20  # /gw/resolve without wait answers in a few seconds
RESOLVE_WAIT_TIMEOUT = 75  # with wait:true the gateway may hold ~45 s for Serper
MAX_RESOLVE_URLS = 20


def is_goto(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.hostname) and bool(GOTO_HOST_RE.match(p.hostname)) \
        and p.path == "/goto"


def goto_label(index: int, displayed: Optional[str] = None, pending: bool = False) -> str:
    """What a search result line shows instead of a ticket URL.

    Google's breadcrumb (`displayedLink`, e.g. "https://host › a › b...") names the
    site even when the real URL is hidden, so it is shown whenever Serper gave one.
    """
    tag = "[goto pending]" if pending else "[goto]"
    if displayed:
        return f"{tag} {displayed}"
    return f"[goto #{index} pending]" if pending else f"[goto #{index}]"


def resolve_via_gateway(urls: list[str], wait: bool = False) -> dict[str, dict]:
    """POST /gw/resolve. Returns {ticket: {"resolved": real|None, "status": ok|cooldown|failed}}.

    Raises RuntimeError with the gateway's message on any non-200.
    """
    config = load_config()
    if not is_gateway(config):
        raise RuntimeError("fcrawl resolve needs gateway mode (gateway_token in .fcrawlrc)")
    out: dict[str, dict] = {}
    for i in range(0, len(urls), MAX_RESOLVE_URLS):
        chunk = urls[i:i + MAX_RESOLVE_URLS]
        resp = requests.post(
            f"{gateway_url(config)}/gw/resolve",
            json={"urls": chunk, "wait": wait},
            headers=gateway_headers(config),
            timeout=RESOLVE_WAIT_TIMEOUT if wait else RESOLVE_TIMEOUT,
        )
        if resp.status_code != 200:
            try:
                message = resp.json().get("error") or f"gateway HTTP {resp.status_code}"
            except ValueError:
                message = f"gateway HTTP {resp.status_code}"
            raise RuntimeError(message)
        for row in resp.json().get("urls") or []:
            out[row["url"]] = {"resolved": row.get("resolved"), "status": row.get("status", "failed")}
    return out


def direct_redeem(url: str, timeout: int = 15) -> tuple[Optional[str], Optional[int]]:
    """Direct mode only (no gateway): redeem a ticket from this machine's own IP.

    One GET without following redirects. Returns (real URL, None) on a 302, or
    (None, seconds) when Google answers the "wait" page whose
    <meta http-equiv="refresh" content="N;url=…"> says how long until it redeems.
    """
    resp = requests.get(url, allow_redirects=False, timeout=timeout,
                        headers={"User-Agent": "Mozilla/5.0 (fcrawl)"})
    if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
        return resp.headers["Location"], None
    match = re.search(r'http-equiv=["\']refresh["\'][^>]*content=["\'](\d+)', resp.text or "", re.IGNORECASE)
    return None, int(match.group(1)) if match else None
