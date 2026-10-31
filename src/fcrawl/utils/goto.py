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


def path_canonical(path: Any) -> str:
    """Same rule as the gateway: repeated percent-decode, backslash as slash, RFC 3986
    dot-segment removal (empty segments count while ".." is applied), then redundant and
    trailing slashes dropped. "/./goto", "/a/../goto", "/goto//.." are all "/goto"."""
    if not isinstance(path, str):
        return ""
    text = path
    for _ in range(3):
        decoded = unquote(text)
        if decoded == text:
            break
        text = decoded
    out: list[str] = []
    for seg in text.replace("\\", "/").split("/"):
        if seg == ".":
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/" + "/".join(s for s in out if s)


def is_goto(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    return p.scheme in ("http", "https") and is_google_host(p.hostname) and path_canonical(p.path) == "/goto"


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
        # every row must be well-formed, answer a ticket of THIS chunk, and answer it once:
        # anything else is a broken gateway, not a ticket that silently went missing
        answers: dict[str, dict] = {}
        for row in payload["urls"]:
            if not isinstance(row, dict) or not isinstance(row.get("url"), str) \
                    or row.get("status") not in ("ok", "cooldown", "failed"):
                raise RuntimeError("gateway resolve returned a malformed row")
            resolved = row.get("resolved")
            if row["status"] == "ok":
                if not isinstance(resolved, str) or not resolved.strip():
                    raise RuntimeError("gateway resolve returned a malformed row")
            elif resolved is not None:
                raise RuntimeError("gateway resolve returned a malformed row")
            if row["url"] not in chunk:
                raise RuntimeError("gateway resolve answered for a ticket that was not asked")
            if row["url"] in answers:
                raise RuntimeError("gateway resolve answered the same ticket twice")
            answers[row["url"]] = {"resolved": resolved if row["status"] == "ok" else None,
                                   "status": row["status"]}
        missing = [u for u in chunk if u not in answers]
        if missing:
            raise RuntimeError(f"gateway resolve answered nothing for {len(missing)} ticket(s)")
        out.update(answers)
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
