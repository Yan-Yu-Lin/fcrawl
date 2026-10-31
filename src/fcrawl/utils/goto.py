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
from urllib.parse import unquote, urlsplit

import idna
import requests

from .config import gateway_headers, gateway_url, is_gateway, load_config

# same rule as the gateway (fcrawl_gw/goto.py): google.<cc>, google.<gtld> or
# google.<sld>.<cc>, any subdomain, trailing dot ignored; google.bad.com is not Google
GOOGLE_DOMAIN_RE = re.compile(r"^google\.(?:com|net|org|[a-z]{2}|(?:com|co|org|net|ne|or|ac)\.[a-z]{2})$")
# the gateway resolves at most this many tickets per call within its own 15 s / 65 s budget
RESOLVE_TIMEOUT = 20  # /gw/resolve without wait
RESOLVE_WAIT_TIMEOUT = 75  # with wait: the gateway may hold ~45 s for Serper on one ticket
MAX_RESOLVE_URLS = 20
RESOLVE_CHUNK = 6  # tickets per /gw/resolve call: the gateway works 6 at a time


def normalize_host(host: Any) -> str:
    """Canonical host like a URL parser would see it (percent-decoded, UTS-46 mapped,
    trailing dots dropped, punycode); '' when unusable. Same rule as the gateway."""
    if not isinstance(host, str) or not host or len(host) > 253:
        return ""
    h = unquote(host.strip())
    if any(ord(c) < 0x21 or c in "/\\?#@[]" for c in h):
        return ""
    try:
        h = idna.uts46_remap(h, std3_rules=False, transitional=False).rstrip(".")
        return idna.encode(h, uts46=True).decode("ascii") if h else ""
    except (idna.IDNAError, UnicodeError, ValueError):
        return ""


def is_google_host(host: Any) -> bool:
    h = normalize_host(host)
    if not h:
        return False
    labels = h.split(".")
    return any(GOOGLE_DOMAIN_RE.match(".".join(labels[i:])) for i in range(len(labels)))


def is_goto(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and is_google_host(p.hostname) and unquote(p.path).rstrip("/") == "/goto"


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
    unique = list(dict.fromkeys(urls))  # one redemption attempt per ticket, across chunks
    for i in range(0, len(unique), RESOLVE_CHUNK):
        chunk = unique[i:i + RESOLVE_CHUNK]
        try:
            resp = requests.post(
                f"{gateway_url(config)}/gw/resolve",
                json={"urls": chunk, "wait": wait},
                headers=gateway_headers(config),
                timeout=RESOLVE_WAIT_TIMEOUT if wait else RESOLVE_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise RuntimeError(f"gateway resolve failed: {type(exc).__name__}: {exc}") from exc
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if resp.status_code != 200:
            message = (payload.get("error") if isinstance(payload, dict) else None) or f"gateway HTTP {resp.status_code}"
            raise RuntimeError(message)
        if not isinstance(payload, dict) or not isinstance(payload.get("urls"), list):
            raise RuntimeError("gateway resolve returned an unexpected response")
        for row in payload["urls"]:
            if isinstance(row, dict) and isinstance(row.get("url"), str):
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
