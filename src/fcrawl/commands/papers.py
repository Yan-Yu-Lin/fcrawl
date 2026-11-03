"""Academic papers: Google Scholar (Serper) + Firecrawl's Research Index, merged.

Two engines with different strengths:

- scholar: Google Scholar through Serper /scholar (gateway: /gw/search type scholar).
  Every field, IEEE/ACM included; year, citation count, PDF link. 1 Serper credit per
  10 results.
- index: Firecrawl's Research Index (~43M abstracts: PubMed, bioRxiv, medRxiv, arXiv).
  Keyless and free, called directly like `fcrawl dev`. Full abstracts, full-text
  passages for a question (`papers read`), citers/references (`papers related`).

`papers pdf` looks for a legal free copy: arXiv, OpenAlex, Semantic Scholar, then a
Scholar PDF link from a trusted host. Never a shadow library.
"""

from __future__ import annotations

import math
import re
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any, Optional
from urllib.parse import quote, unquote, urlsplit

import click
import requests
from rich.markup import escape

from ..utils import institution
from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.config import is_gateway
from ..utils.output import Progress, SpinnerColumn, TextColumn, handle_output
from ..utils.output import err_console as console  # notes and credits: stderr, so stdout stays parseable
from .dev import _get_cloud_api_key
from .search import SEARCH_GATEWAY_TIMEOUT, _parse_locale, serper_target

RESEARCH_ENDPOINT = "https://api.firecrawl.dev/v2/search/research/papers"
SERPER_SCHOLAR_ENDPOINT = "https://google.serper.dev/scholar"
OPENALEX_ENDPOINT = "https://api.openalex.org/works"
S2_ENDPOINT = "https://api.semanticscholar.org/graph/v1/paper"
SCHOLAR_PAGE = 10  # Google Scholar answers at most 10 per page whatever `num` asks
INDEX_TIMEOUT = 45
LOOKUP_TIMEOUT = 20
SNIPPET_CHARS = 300
CACHE_BUCKET = "papers"
ENGINES = ("all", "scholar", "index")
YEAR_RANGE = (1800, 2200)  # what the gateway accepts for as_ylo / as_yhi


class PapersError(RuntimeError):
    """A failure worth one readable line (no traceback). `credits`: Serper credits the
    failed call had already spent (a later Scholar page failing after earlier ones)."""

    def __init__(self, message: str, credits: int = 0):
        super().__init__(message)
        self.credits = credits


class NotInIndex(PapersError):
    """The Research Index does not know this id."""


# --- ids ---------------------------------------------------------------------

_ARXIV_NEW = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")
_ARXIV_OLD = re.compile(r"^[a-z-]+(\.[A-Z]{2})?/\d{7}(v\d+)?$")
_DOI = re.compile(r"10\.\d{4,9}/[^\s?#\"]+")
_DOI_TAIL = re.compile(r"(\.pdf|/full|/abstract|/pdf|/epdf|/html|/meta)+$", re.I)
_ARXIV_PATH = re.compile(r"^/(?:abs|pdf|html)/(.+?)(?:\.pdf)?/?$", re.I)
_PMC_PATH = re.compile(r"^(?:/pmc)?/articles/(PMC\d+|\d+)", re.I)
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _url_parts(text: str) -> tuple[str, str]:
    """(host, decoded path) of an http(s) URL; query and fragment are never part of an id."""
    try:
        u = urlsplit(text)
    except ValueError:
        return "", ""
    if u.scheme.lower() not in ("http", "https"):
        return "", ""
    return (u.hostname or "").lower().rstrip("."), unquote(u.path)


def ids_from_url(url: Optional[str]) -> dict[str, str]:
    """arxiv / pmcid / pmid / doi found in a paper URL (arxiv.org, PMC, PubMed, doi.org,
    publisher pages with the DOI in the path)."""
    if not isinstance(url, str):
        return {}
    host, path = _url_parts(url)
    if not host:
        return {}
    ids: dict[str, str] = {}
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        m = _ARXIV_PATH.match(path)
        if m:
            ids["arxiv"] = m.group(1)
    if host in ("europepmc.org", "www.europepmc.org"):
        m = re.match(r"^/(?:abstract|article)/(PMC|MED)/(PMC\d+|\d+)/?", path, re.I)
        if m and m.group(1).upper() == "PMC":
            v = m.group(2).upper()
            ids["pmcid"] = v if v.startswith("PMC") else "PMC" + v
        elif m and m.group(2).isdigit() and len(m.group(2)) <= 9:
            ids["pmid"] = m.group(2)
    if host in ("pmc.ncbi.nlm.nih.gov", "www.ncbi.nlm.nih.gov", "ncbi.nlm.nih.gov"):
        m = _PMC_PATH.match(path)
        if m:
            v = m.group(1).upper()
            ids["pmcid"] = v if v.startswith("PMC") else "PMC" + v
    if host == "pubmed.ncbi.nlm.nih.gov":
        m = re.match(r"^/(\d{1,9})/?$", path)
        if m:
            ids["pmid"] = m.group(1)
    doi = doi_in(path)
    if doi:
        ids["doi"] = doi
    return ids


def normalize_id(raw: str) -> str:
    """User input -> a Research Index id: arxiv:, doi:, pmid:, pmcid:, or a canonical
    paperId. Accepts the prefixed forms, bare arXiv ids / DOIs / PMC ids, and
    arxiv.org / doi.org / PMC URLs."""
    text = (raw or "").strip()
    if not text or len(text) > 300 or any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise click.BadParameter("not a paper id", param_hint="ID")
    low = text.lower()
    if low.startswith(("http://", "https://")):
        ids = ids_from_url(text)
        for k in ("arxiv", "pmcid", "pmid", "doi"):
            if ids.get(k):
                return f"{k}:{ids[k]}"
        raise click.BadParameter("no arXiv id, DOI or PubMed id in this URL", param_hint="ID")
    for prefix in ("arxiv:", "doi:", "pmid:", "pmcid:"):
        if low.startswith(prefix):
            value = text[len(prefix):].strip()
            if not value:
                raise click.BadParameter("empty id", param_hint="ID")
            if prefix == "pmcid:" and not value.upper().startswith("PMC"):
                value = "PMC" + value
            return prefix + (value.upper() if prefix == "pmcid:" else value)
    if _ARXIV_NEW.match(text) or _ARXIV_OLD.match(text):
        return "arxiv:" + text
    if low.startswith("10."):
        return "doi:" + text
    if re.fullmatch(r"pmc\d+", low):
        return "pmcid:" + text.upper()
    if text.isdigit():
        # PubMed ids are at most 9 digits; the index's canonical paperIds are longer
        return ("pmid:" + text) if len(text) <= 9 else text
    raise click.BadParameter("use arxiv:ID, doi:DOI, pmid:N, pmcid:PMCN, or a paper URL", param_hint="ID")


def id_path(paper_id: str) -> str:
    """The id as one URL path segment: a DOI's '/' must be %2F or the index 404s."""
    return quote(paper_id, safe=":")


def doi_in(text: Optional[str]) -> Optional[str]:
    """A DOI inside a URL path (doi.org, Springer, ACM, Wiley, T&F...), lowercased."""
    if not isinstance(text, str):
        return None
    m = _DOI.search(text)
    if not m:
        return None
    doi = _DOI_TAIL.sub("", m.group(0)).rstrip(".,;")
    # a closing bracket belongs to the DOI only when it closes one inside it: SICI DOIs
    # like 10.1002/(SICI)1099-0844(199912)17:4<290::AID-CBF849>3.0.CO;2-P keep theirs
    while doi and doi[-1] in ")]" and doi.count("(") + doi.count("[") < doi.count(")") + doi.count("]"):
        doi = doi[:-1].rstrip(".,;")
    return doi.lower() if "/" in doi and len(doi) > 7 else None


def strip_version(arxiv_id: str) -> str:
    return re.sub(r"v\d+$", "", arxiv_id)


def arxiv_year(arxiv_id: Optional[str]) -> Optional[int]:
    """New-style arXiv ids start with YYMM (since 2007)."""
    if arxiv_id and re.match(r"^\d{4}\.\d{4,5}", arxiv_id):
        yy = int(arxiv_id[:2])
        if 7 <= yy <= 99:
            return 2000 + yy
    return None


def preferred_id(ids: dict) -> Optional[str]:
    """The id to hand to read/show/related/pdf: arXiv, then DOI, PMC, PubMed."""
    for k in ("arxiv", "doi", "pmcid", "pmid"):
        if ids.get(k):
            return f"{k}:{ids[k]}"
    return None


def id_links(ids: dict) -> dict:
    """Landing page and PDF we can build from ids alone."""
    links: dict[str, str] = {}
    if ids.get("arxiv"):
        links["page"] = f"https://arxiv.org/abs/{ids['arxiv']}"
        links["pdf"] = f"https://arxiv.org/pdf/{ids['arxiv']}"
    elif ids.get("pmcid"):
        links["page"] = f"https://pmc.ncbi.nlm.nih.gov/articles/{ids['pmcid']}/"
    elif ids.get("pmid"):
        links["page"] = f"https://pubmed.ncbi.nlm.nih.gov/{ids['pmid']}/"
    elif ids.get("doi"):
        links["page"] = f"https://doi.org/{ids['doi']}"
    if ids.get("doi"):
        links["doi"] = f"https://doi.org/{ids['doi']}"
    return links


def title_norm(title: Any) -> str:
    if not isinstance(title, str):
        return ""
    t = unicodedata.normalize("NFKC", title).casefold()
    t = re.sub(r"[^\w\s]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def clean(text: Any) -> str:
    """API text for the terminal: no control characters (escape sequences), newlines kept."""
    return _CONTROL.sub("", text) if isinstance(text, str) else ""


def out(text: str = "") -> None:
    """Every human-readable stdout line goes through here: API strings end up in ids,
    links and hints too, and none of them may carry terminal escape sequences."""
    print(clean(text))


def one_line(text: Any) -> str:
    return re.sub(r"\s+", " ", clean(text)).strip()


def shorten(text: str, n: int = SNIPPET_CHARS) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --- dates -------------------------------------------------------------------


def parse_bound(value: Optional[str], end: bool, hint: str) -> tuple[Optional[int], Optional[str]]:
    """--since/--until: YYYY or YYYY-MM-DD -> (year for Scholar, date for the index)."""
    if not value:
        return None, None
    v = value.strip()
    if re.fullmatch(r"\d{4}", v):
        year = int(v)
        day = f"{v}-12-31" if end else f"{v}-01-01"
    else:
        try:
            d = date.fromisoformat(v)
        except ValueError:
            raise click.BadParameter("use a year (2024) or a date (2024-05-01)", param_hint=hint) from None
        year, day = d.year, d.isoformat()
    if not YEAR_RANGE[0] <= year <= YEAR_RANGE[1]:
        raise click.BadParameter("year out of range", param_hint=hint)
    return year, day


# --- HTTP --------------------------------------------------------------------


def _cached(key: str, no_cache: bool) -> Optional[dict]:
    return None if no_cache else read_cache(CACHE_BUCKET, key)


def index_get(path: str, params: dict) -> dict:
    """GET on the Research Index. Keyless; a configured Firecrawl Cloud key (fc-…) only
    raises the rate limit. The gateway token never goes here."""
    headers: dict[str, str] = {}
    key = _get_cloud_api_key()
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = RESEARCH_ENDPOINT + path
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=INDEX_TIMEOUT)
        if resp.status_code == 401 and key:
            resp = requests.get(url, params=params, headers={}, timeout=INDEX_TIMEOUT)
    except requests.RequestException as exc:
        raise PapersError(f"Research Index unreachable ({type(exc).__name__})") from None
    if resp.status_code == 404:
        raise NotInIndex("not in the Research Index (it covers PubMed, bioRxiv, medRxiv and arXiv)")
    if resp.status_code == 429:
        raise PapersError("Research Index rate limit (keyless, per IP); wait a minute and retry")
    try:
        data = resp.json()
    except ValueError:
        data = None
    if resp.status_code != 200 or not isinstance(data, dict) or not data.get("success", False):
        detail = ""
        if isinstance(data, dict):
            detail = str(data.get("error") or "")
            details = data.get("details")
            if isinstance(details, list) and details and isinstance(details[0], dict):
                detail += f" ({details[0].get('message', '')})"
        raise PapersError(f"Research Index error {resp.status_code}: {detail[:200] or resp.text[:200]}")
    return data


def scholar_search(query: str, limit: int, ylo: Optional[int], yhi: Optional[int],
                   hl: Optional[str], no_cache: bool) -> tuple[list[dict], int, bool]:
    """Google Scholar through Serper. Returns (organic items, Serper credits spent, any
    page served from the gateway's cache). Gateway mode: /gw/search type scholar with
    the family token; direct mode: Serper /scholar with serper_api_key. A PapersError
    carries the credits earlier pages already spent."""
    endpoint, api_key = serper_target()
    gateway = is_gateway()
    if not gateway:
        endpoint = SERPER_SCHOLAR_ENDPOINT
    if not api_key:
        raise PapersError("Scholar needs a Serper key (serper_api_key in .fcrawlrc) or a gateway token")
    headers = {"X-API-KEY": api_key, "Content-Type": "application/json"}
    if no_cache:
        headers["X-No-Cache"] = "1"
    items: list[dict] = []
    spent = {"credits": 0, "cached": False}
    try:
        for page in range(1, max(1, math.ceil(limit / SCHOLAR_PAGE)) + 1):
            organic = _scholar_page(endpoint, headers, gateway, query, page, ylo, yhi, hl, spent)
            items.extend(organic)
            if len(organic) < SCHOLAR_PAGE or len(items) >= limit:
                break
    except PapersError as exc:
        exc.credits += spent["credits"]
        raise
    return items[:limit], spent["credits"], spent["cached"]


def _billed(body: Any) -> int:
    """Credits a failed gateway answer says were spent anyway (details.creditsUsed)."""
    details = body.get("details") if isinstance(body, dict) else None
    used = details.get("creditsUsed") if isinstance(details, dict) else None
    return used if type(used) is int and 0 <= used <= 10 else 0


def _scholar_page(endpoint: str, headers: dict, gateway: bool, query: str, page: int,
                  ylo: Optional[int], yhi: Optional[int], hl: Optional[str], spent: dict) -> list[dict]:
    payload: dict[str, Any] = {"q": query, "num": SCHOLAR_PAGE}
    if page > 1:
        payload["page"] = page
    if gateway:
        payload["type"] = "scholar"
    if ylo is not None:
        payload["as_ylo"] = ylo
    if yhi is not None:
        payload["as_yhi"] = yhi
    if hl:
        payload["hl"] = hl
    try:
        resp = requests.post(endpoint, headers=headers, json=payload,
                             timeout=SEARCH_GATEWAY_TIMEOUT if gateway else 30)
    except requests.RequestException as exc:
        raise PapersError(f"Scholar request failed ({type(exc).__name__})") from None
    if resp.status_code != 200:
        detail = resp.text[:160]
        body = None
        try:
            body = resp.json()
            detail = str(body.get("error") or body.get("message") or detail)
        except (ValueError, AttributeError):
            pass
        if gateway:
            spent["credits"] += _billed(body)
            if resp.status_code == 400 and "search or news" in detail:
                raise PapersError("this gateway does not support Scholar yet; ask Arthur to update it")
        if resp.status_code == 429 and resp.headers.get("Retry-After"):
            detail += f" (retry after {resp.headers['Retry-After']}s)"
        raise PapersError(f"Scholar error {resp.status_code}: {detail}")
    try:
        data = resp.json()
    except ValueError:
        raise PapersError("Scholar returned non-JSON") from None
    if not isinstance(data, dict):
        raise PapersError("Scholar returned an unexpected shape")
    gw = data.get("gw") if isinstance(data.get("gw"), dict) else {}
    if gateway:
        if gw and gw.get("gwType") != "scholar":
            raise PapersError("this gateway answered with web results; ask Arthur to update it")
        used = gw.get("gwCreditsUsed")
        if gw.get("gwCached"):
            spent["cached"] = True  # a gateway cache hit is free whatever else it says
        else:
            spent["credits"] += used if type(used) is int and used >= 0 else 1
    else:
        c = data.get("credits")
        spent["credits"] += c if type(c) is int and c >= 0 else 1
    return [it for it in data.get("organic") or [] if isinstance(it, dict)]


# --- normalisation -----------------------------------------------------------


def from_scholar(it: dict) -> dict:
    link = it.get("link") if isinstance(it.get("link"), str) else None
    pdf = it.get("pdfUrl") if isinstance(it.get("pdfUrl"), str) else None
    ids = ids_from_url(link)
    for k, v in ids_from_url(pdf).items():
        ids.setdefault(k, v)
    if ids.get("arxiv"):
        ids["arxiv"] = strip_version(ids["arxiv"])
    year = it.get("year") if type(it.get("year")) is int else None
    cited = it.get("citedBy") if type(it.get("citedBy")) is int else None
    return {
        "title": one_line(it.get("title")),
        "year": year,
        "venue": one_line(it.get("publicationInfo")) or None,
        "citedBy": cited,
        "ids": ids,
        "links": {k: v for k, v in (("page", link), ("pdf", pdf)) if v},
        "snippet": one_line(it.get("snippet")) or None,
        "abstract": None,
        "sources": ["scholar"],
    }


def from_index(it: dict) -> dict:
    ids: dict[str, str] = {}
    raw = it.get("ids") if isinstance(it.get("ids"), dict) else {}
    for k in ("arxiv", "doi", "pmcid", "pmid"):
        v = raw.get(k)
        if isinstance(v, list) and v and isinstance(v[0], str):
            ids[k] = v[0]
    primary = it.get("primaryId") if isinstance(it.get("primaryId"), str) else ""
    if ":" in primary:
        k, _, v = primary.partition(":")
        if k in ("arxiv", "doi", "pmcid", "pmid") and v:
            ids.setdefault(k, v)
    if ids.get("doi"):
        ids["doi"] = ids["doi"].lower()
    if ids.get("arxiv"):
        ids["arxiv"] = strip_version(ids["arxiv"])
    links = id_links(ids)
    return {
        "title": one_line(it.get("title")),
        "year": arxiv_year(ids.get("arxiv")),
        "venue": None,
        "citedBy": None,
        "ids": ids,
        "id": primary or preferred_id(ids),
        "links": {k: links[k] for k in ("page", "pdf") if k in links},
        "snippet": None,
        "abstract": one_line(it.get("abstract")) or None,
        "score": it.get("score") if isinstance(it.get("score"), (int, float)) else None,
        "paperId": it.get("paperId") if isinstance(it.get("paperId"), str) else None,
        "sources": ["index"],
    }


def _id_keys(p: dict) -> set[str]:
    """Every id this record was ever known by (a merged record keeps the aliases its
    parts brought, e.g. a preprint DOI and the publication DOI), for matching only."""
    keys = {f"{k}:{str(v).lower()}" for k, v in p["ids"].items() if v}
    return keys | set(p.get("_aliases") or ())


def _same_title(a: dict, b: dict) -> bool:
    """Title match across engines. Within one engine two equal titles stay two papers,
    and short titles never fold ("Point transformer" is two different 2021 papers).
    Years must not disagree by more than one (preprint vs venue)."""
    if set(a["sources"]) & set(b["sources"]):
        return False
    ta, tb = title_norm(a["title"]), title_norm(b["title"])
    if len(ta.split()) < 4 or ta != tb:
        return False
    return a["year"] is None or b["year"] is None or abs(a["year"] - b["year"]) <= 1


def _absorb(into: dict, other: dict) -> None:
    into["_aliases"] = sorted(_id_keys(into) | _id_keys(other))
    for s in other["sources"]:
        if s not in into["sources"]:
            into["sources"].append(s)
    for k, v in other["ids"].items():
        into["ids"].setdefault(k, v)
    for k, v in other["links"].items():
        into["links"].setdefault(k, v)
    for k in ("year", "venue", "citedBy", "snippet", "abstract", "score", "paperId"):
        if into.get(k) is None and other.get(k) is not None:
            into[k] = other[k]
    if other.get("id") and "index" in other["sources"]:
        into["id"] = other["id"]  # the index's own primaryId is what read/show accept


def merge(*lists: list[dict]) -> list[dict]:
    """Interleave the engines rank by rank (each engine's order is its relevance), and
    fold duplicates (same DOI / arXiv / PubMed id, or same title across engines) into
    the first occurrence. A record that links two earlier ones (a DOI one and an arXiv
    one) folds all three together."""
    merged: list[dict] = []
    for rank in range(max((len(x) for x in lists), default=0)):
        for lst in lists:
            if rank >= len(lst):
                continue
            p = lst[rank]
            keys = _id_keys(p)
            hits = [o for o in merged if keys & _id_keys(o)]
            if not hits:
                hits = [o for o in merged if _same_title(o, p)][:1]
            if not hits:
                merged.append(p)
                continue
            first = hits[0]
            _absorb(first, p)
            for other in hits[1:]:
                _absorb(first, other)
                merged.remove(other)
    for p in merged:
        p.pop("_aliases", None)
        p.setdefault("id", None)
        if not p["id"]:
            p["id"] = preferred_id(p["ids"])
    return merged


def sort_papers(papers: list[dict], how: str) -> list[dict]:
    if how == "citations":
        return sorted(papers, key=lambda p: -(p["citedBy"] if p["citedBy"] is not None else -1))
    if how == "date":
        return sorted(papers, key=lambda p: -(p["year"] if p["year"] is not None else -1))
    return papers


# --- output ------------------------------------------------------------------


def print_paper(n: int, p: dict, abstracts: bool) -> None:
    out(f"[{n}] {one_line(p['title']) or '(no title)'}")
    facts = []
    if p.get("year"):
        facts.append(str(p["year"]))
    if p.get("citedBy") is not None:
        facts.append(f"cited {p['citedBy']}")
    facts.append("+".join(p["sources"]))
    if p.get("id"):
        facts.append(p["id"])
    out("    " + " · ".join(facts))
    if p.get("venue"):
        out(f"    {one_line(p['venue'])}")
    if p["links"].get("page"):
        out(f"    {one_line(p['links']['page'])}")
    if p["links"].get("pdf"):
        out(f"    pdf: {one_line(p['links']['pdf'])}")
    text = p.get("abstract") or p.get("snippet")
    if text:
        text = one_line(text)
        out("    " + (text if abstracts else shorten(text)))


def print_hints(papers: list[dict]) -> None:
    """What an agent can do next, with real ids from this listing."""
    lines = []
    # never suggest scraping a ResearchGate/Academia upload: copyright unclear
    pdf = next((p["links"]["pdf"] for p in papers
                if p["links"].get("pdf") and host_kind(p["links"]["pdf"]) != "social-upload"), None)
    if pdf:
        lines.append(f"read a PDF:      fcrawl scrape {pdf} --save-temp")
    indexed = next((p["id"] for p in papers if "index" in p["sources"] and p.get("id")), None)
    if indexed:
        lines.append(f"ask one paper:   fcrawl papers read {indexed} -q \"your question\"")
        lines.append(f"citing/related:  fcrawl papers related {indexed} --mode citers")
    doi = next((f"doi:{p['ids']['doi']}" for p in papers if p["ids"].get("doi") and not p["links"].get("pdf")), None)
    if doi:
        lines.append(f"free copy?       fcrawl papers pdf {doi}")
    if lines:
        out("---")
        for line in lines:
            out(one_line(line))


def emit_json(data: dict, output: Optional[str]) -> None:
    handle_output(data, output_file=output, json_output=True, pretty=False, format_type="json")


def credits_note(scholar_credits: Optional[int], cached: bool) -> str:
    if scholar_credits is None:
        return "credits: 0 (Research Index is free)"
    note = f"credits: {scholar_credits} Serper (Scholar)"
    if cached:
        note += ", cached"
    return note


# --- CLI ---------------------------------------------------------------------


class PapersGroup(click.Group):
    """`fcrawl papers "query"` means `fcrawl papers search "query"`."""

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        if args and args[0] not in self.commands and args[0] != "--help":
            args = ["search", *args]
        return super().parse_args(ctx, args)


@click.group(cls=PapersGroup)
def papers():
    """Academic papers: Google Scholar + Firecrawl Research Index.

    \b
    fcrawl papers "query"                      # both engines, merged and deduped
    fcrawl papers "query" --engine index       # free index only (biomed + arXiv)
    fcrawl papers show arxiv:1706.03762        # metadata + full abstract
    fcrawl papers read arxiv:1706.03762 -q "what is multi-head attention"
    fcrawl papers related arxiv:1706.03762 --mode citers
    fcrawl papers pdf doi:10.1038/s41586-021-03819-2   # legal free copy
    """


@papers.command("search")
@click.argument("query")
@click.option("--engine", "-e", type=click.Choice(ENGINES), default="all", show_default=True,
              help="scholar = Google Scholar (1 Serper credit per 10); index = Research Index (free)")
@click.option("--limit", "-l", type=click.IntRange(1, 50), default=10, show_default=True,
              help="Papers to show (Scholar: every 10 more = 1 more credit)")
@click.option("--since", default=None, help="From year or date: 2024 or 2024-05-01")
@click.option("--until", default=None, help="Up to year or date: 2025 or 2025-06-30")
@click.option("--authors", default=None, help="Index only: author name substring")
@click.option("--category", "categories", multiple=True, help="Index only: e.g. cs.LG (repeatable)")
@click.option("--sort", "sort_by", type=click.Choice(["relevance", "citations", "date"]), default="relevance",
              show_default=True, help="citations/date re-sort what was found; they do not search wider")
@click.option("--locale", "-L", default=None, help="Scholar interface language, e.g. zh-TW (default: none)")
@click.option("--abstracts", is_flag=True, help="Full abstracts instead of the first 300 characters")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("-o", "--output", help="Save JSON to a file")
@click.option("--no-cache", is_flag=True, help="Bypass caches (local and gateway)")
def search_cmd(query: str, engine: str, limit: int, since: Optional[str], until: Optional[str],
               authors: Optional[str], categories: tuple[str, ...], sort_by: str, locale: Optional[str],
               abstracts: bool, json_output: bool, output: Optional[str], no_cache: bool):
    """Search papers (default subcommand: `fcrawl papers "query"` works too)."""
    query = query.strip()
    if not query or len(query) > 500:
        raise click.BadParameter("1-500 characters", param_hint="QUERY")
    ylo, day_from = parse_bound(since, end=False, hint="--since")
    yhi, day_to = parse_bound(until, end=True, hint="--until")
    if day_from and day_to and day_from > day_to:
        raise click.BadParameter("--since is after --until", param_hint="--since")
    hl = _parse_locale(locale, query)[1] if locale else None
    if engine == "scholar" and (authors or categories):
        console.print("[dim]--authors/--category only filter the Research Index; ignored for Scholar[/dim]")

    errors: dict[str, str] = {}
    scholar_credits: Optional[int] = None
    gw_cached = False
    from_cache = {"scholar": False, "index": False}

    def run_scholar() -> list[dict]:
        nonlocal scholar_credits, gw_cached
        key = cache_key(query, {"engine": "scholar", "limit": limit, "ylo": ylo, "yhi": yhi, "hl": hl})
        hit = _cached(key, no_cache)
        if hit is not None:
            from_cache["scholar"] = True
            scholar_credits = 0
            return hit["items"]
        items, scholar_credits, gw_cached = scholar_search(query, limit, ylo, yhi, hl, no_cache)
        write_cache(CACHE_BUCKET, key, {"items": items})
        return items

    def run_index() -> list[dict]:
        params: dict[str, Any] = {"query": query, "k": limit}
        if authors:
            params["authors"] = authors
        if categories:
            params["categories"] = ",".join(categories)
        if day_from:
            params["from"] = day_from
        if day_to:
            params["to"] = day_to
        key = cache_key(query, {"engine": "index", **params})
        hit = _cached(key, no_cache)
        if hit is not None:
            from_cache["index"] = True
            return hit["items"]
        data = index_get("", params)
        items = [r for r in data.get("results") or [] if isinstance(r, dict)]
        write_cache(CACHE_BUCKET, key, {"items": items})
        return items

    wanted = ["scholar", "index"] if engine == "all" else [engine]
    if "scholar" in wanted and engine == "all" and not serper_target()[1]:
        wanted.remove("scholar")
        errors["scholar"] = "skipped: no Serper key and no gateway token"
    runners = {"scholar": run_scholar, "index": run_index}
    raw: dict[str, list[dict]] = {}
    with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console) as prog:
        prog.add_task(f"Searching papers for '{escape(query)}'...", total=None)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {name: pool.submit(runners[name]) for name in wanted}
            for name, fut in futures.items():
                try:
                    raw[name] = fut.result()
                except PapersError as exc:
                    errors[name] = str(exc)
                    if name == "scholar" and exc.credits:
                        scholar_credits = exc.credits  # earlier pages were billed before the failure
    for name, msg in errors.items():
        if not msg.startswith("skipped") or engine != "all":
            console.print(f"[yellow]{name}: {escape(msg)}[/yellow]", highlight=False)
    if not raw:
        if scholar_credits:
            # paid pages before the failure: still say what they cost
            console.print(f"[dim]{credits_note(scholar_credits, gw_cached)}[/dim]", highlight=False)
        console.print("[red]No engine answered[/red]")
        raise click.Abort()

    lists = [[from_scholar(it) for it in raw.get("scholar", [])], [from_index(it) for it in raw.get("index", [])]]
    found = sort_papers(merge(*lists), sort_by)[:limit]
    console.print(f"[dim]{credits_note(scholar_credits, gw_cached)}[/dim]", highlight=False)

    if json_output or output:
        emit_json({
            "query": query,
            "engines": list(raw),
            "results": found,
            "meta": {"sort": sort_by, "since": since, "until": until, "fromCache": from_cache,
                     "scholarCredits": scholar_credits or 0, "errors": errors},
        }, output)
        return
    if not found:
        console.print("[yellow]No papers found[/yellow]")
        return
    console.print(f"[green]Found {len(found)} papers[/green] [dim]({' + '.join(raw)})[/dim]", highlight=False)
    for n, p in enumerate(found, start=1):
        print_paper(n, p, abstracts)
        out()
    print_hints(found)


def _paper_meta(paper_id: str, no_cache: bool) -> dict:
    key = cache_key(paper_id, {"engine": "index", "op": "show"})
    hit = _cached(key, no_cache)
    if hit is not None:
        return hit
    data = index_get("/" + id_path(paper_id), {})
    paper = data.get("paper") if isinstance(data.get("paper"), dict) else None
    if not paper:
        raise PapersError("Research Index returned no paper")
    write_cache(CACHE_BUCKET, key, paper)
    return paper


def _ids_of(paper: dict) -> dict:
    return from_index({"ids": paper.get("ids"), "primaryId": ""})["ids"]


@papers.command("show")
@click.argument("paper_id", metavar="ID")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--no-cache", is_flag=True, help="Bypass the local cache")
def show_cmd(paper_id: str, json_output: bool, no_cache: bool):
    """Metadata and full abstract of one paper (Research Index; OpenAlex for other DOIs)."""
    pid = normalize_id(paper_id)
    try:
        paper = _paper_meta(pid, no_cache)
        source = "index"
    except NotInIndex:
        if not pid.startswith(("doi:", "pmid:", "pmcid:")):
            console.print(f"[red]{escape(pid)}: not in the Research Index[/red]", highlight=False)
            raise click.Abort()
        try:
            paper = openalex_work(pid, no_cache)
        except PapersError as exc:
            console.print(f"[red]{escape(pid)}: {escape(str(exc))}[/red]", highlight=False)
            raise click.Abort()
        source = "openalex"
    except PapersError as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]", highlight=False)
        raise click.Abort()

    if source == "openalex":
        ids = paper["ids"]
        info = {"id": pid, "source": "openalex", **paper, "links": id_links(ids)}
    else:
        ids = _ids_of(paper)
        info = {"id": pid, "source": "index", "title": one_line(paper.get("title")),
               "authors": one_line(paper.get("authors")) or None,
               "categories": paper.get("categories") if isinstance(paper.get("categories"), list) else [],
               "created": paper.get("createdDate"), "updated": paper.get("updateDate"),
               "ids": ids, "paperId": paper.get("paperId"), "abstract": (paper.get("abstract") or "").strip(),
               "links": id_links(ids)}
    if json_output:
        emit_json(info, None)
        return
    out(one_line(info["title"]) or "(no title)")
    if info.get("authors"):
        out(f"authors: {one_line(info['authors'])}")
    facts = [f"{k}: {info[k]}" for k in ("year", "venue", "citedBy", "created", "updated") if info.get(k)]
    if info.get("categories"):
        facts.append("categories: " + ", ".join(map(str, info["categories"])))
    if info.get("oaStatus"):
        facts.append(f"open access: {info['oaStatus']}")
    if facts:
        out(one_line(" · ".join(facts)))
    out("ids: " + " · ".join(f"{k}:{v}" for k, v in ids.items()) + f"  (via {source})")
    for k, v in info["links"].items():
        if k == "doi" and v == info["links"].get("page"):
            continue
        out(f"{k}: {v}")
    out()
    out(clean(info.get("abstract")) or "(no abstract)")
    out("---")
    if source == "index":
        out(f"ask it:   fcrawl papers read {pid} -q \"your question\"")
        out(f"related:  fcrawl papers related {pid} --mode citers")
    if "pdf" in info["links"]:
        out(f"read PDF: fcrawl scrape {info['links']['pdf']} --save-temp")
    elif ids.get("doi"):
        out(f"free copy? fcrawl papers pdf doi:{ids['doi']}")


@papers.command("read")
@click.argument("paper_id", metavar="ID")
@click.option("--question", "-q", required=True, help="What you want to know from this paper")
@click.option("--passages", "-k", type=click.IntRange(1, 50), default=4, show_default=True,
              help="Passages to return")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--no-cache", is_flag=True, help="Bypass the local cache")
def read_cmd(paper_id: str, question: str, passages: int, json_output: bool, no_cache: bool):
    """Full-text passages of one paper that answer a question (Research Index)."""
    pid = normalize_id(paper_id)
    question = question.strip()
    if not question or len(question) > 500:
        raise click.BadParameter("1-500 characters", param_hint="--question")
    key = cache_key(pid, {"engine": "index", "op": "read", "q": question, "k": passages})
    data = _cached(key, no_cache)
    if data is None:
        try:
            data = index_get("/" + id_path(pid), {"query": question, "k": passages})
        except PapersError as exc:
            console.print(f"[red]{escape(pid)}: {escape(str(exc))}[/red]", highlight=False)
            raise click.Abort()
        write_cache(CACHE_BUCKET, key, data)
    paper = data.get("paper") if isinstance(data.get("paper"), dict) else {}
    found = [p for p in data.get("passages") or [] if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if json_output:
        emit_json({"id": pid, "title": paper.get("title"), "question": question, "passages": found}, None)
        return
    out(f"{one_line(paper.get('title')) or pid}  ({pid})")
    out(f"Q: {question}")
    if not found:
        out()
        out("No full-text passages for this paper (the index has full text mainly for arXiv papers).")
        out(f"Abstract: fcrawl papers show {pid}   ·   PDF: fcrawl papers pdf {pid}")
        return
    for n, p in enumerate(found, start=1):
        score = p.get("score")
        out()
        out(f"[{n}]" + (f" score {score:.3f}" if isinstance(score, (int, float)) else ""))
        out(clean(p["text"]).strip())


@papers.command("related")
@click.argument("paper_id", metavar="ID")
@click.option("--mode", "-m", type=click.Choice(["similar", "citers", "references"]), default="similar",
              show_default=True, help="similar = co-citation neighbours; citers = papers citing it; references")
@click.option("--intent", "-i", default=None, help="What you are looking for (default: the paper's own title)")
@click.option("--limit", "-l", type=click.IntRange(1, 100), default=10, show_default=True)
@click.option("--abstracts", is_flag=True, help="Full abstracts instead of the first 300 characters")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--no-cache", is_flag=True, help="Bypass the local cache")
def related_cmd(paper_id: str, mode: str, intent: Optional[str], limit: int, abstracts: bool,
                json_output: bool, no_cache: bool):
    """Papers related to one paper: similar, citing it, or cited by it (Research Index)."""
    pid = normalize_id(paper_id)
    try:
        if not intent:
            intent = one_line(_paper_meta(pid, no_cache).get("title")) or pid
        intent = intent.strip()[:500]
        params = {"intent": intent, "mode": mode, "k": limit}
        key = cache_key(pid, {"engine": "index", "op": "related", **params})
        data = _cached(key, no_cache)
        if data is None:
            data = index_get("/" + id_path(pid) + "/similar", params)
            write_cache(CACHE_BUCKET, key, data)
    except PapersError as exc:
        console.print(f"[red]{escape(pid)}: {escape(str(exc))}[/red]", highlight=False)
        raise click.Abort()
    found = [from_index(r) for r in data.get("results") or [] if isinstance(r, dict)]
    if json_output:
        emit_json({"id": pid, "mode": mode, "intent": intent, "results": found,
                   "poolSize": data.get("poolSize"), "truncated": data.get("truncated")}, None)
        return
    if not found:
        console.print(f"[yellow]No {mode} found for {escape(pid)} in the Research Index[/yellow]", highlight=False)
        return
    console.print(f"[green]{len(found)} {mode} of {escape(pid)}[/green] [dim](intent: {escape(intent[:80])})[/dim]",
                  highlight=False)
    for n, p in enumerate(found, start=1):
        print_paper(n, p, abstracts)
        out()
    print_hints(found)


# --- legal free copy -------------------------------------------------------------

REPOSITORY_HOSTS = (
    "arxiv.org", "ncbi.nlm.nih.gov", "europepmc.org", "biorxiv.org", "medrxiv.org", "zenodo.org",
    "hal.science", "archives-ouvertes.fr", "osf.io", "core.ac.uk", "openreview.net", "aclanthology.org",
    "proceedings.mlr.press", "proceedings.neurips.cc", "papers.nips.cc", "openaccess.thecvf.com",
    "jmlr.org", "ceur-ws.org", "eprint.iacr.org", "semanticscholar.org", "escholarship.org",
)
OA_PUBLISHER_HOSTS = (
    "mdpi.com", "frontiersin.org", "plos.org", "peerj.com", "hindawi.com", "biomedcentral.com",
    "springeropen.com", "elifesciences.org", "copernicus.org", "scielo.org", "doaj.org", "jstage.jst.go.jp",
    "ojs.aaai.org", "usenix.org", "pnas.org", "sciendo.com", "cell.com",
)
SOCIAL_HOSTS = ("researchgate.net", "academia.edu")
_ACADEMIC_TLD = re.compile(r"(\.edu|\.edu\.[a-z]{2}|\.ac\.[a-z]{2}|\.uni-[a-z-]+\.de)$")


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def _under(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def host_kind(url: str) -> str:
    """repository / publisher-oa / author-site / social-upload / unknown, by host only."""
    host = _host(url)
    if not host or urlsplit(url).scheme not in ("http", "https"):
        return "unknown"
    if _under(host, REPOSITORY_HOSTS):
        return "repository"
    if _under(host, OA_PUBLISHER_HOSTS):
        return "publisher-oa"
    if _under(host, SOCIAL_HOSTS):
        return "social-upload"
    if _ACADEMIC_TLD.search(host):
        return "author-site"
    return "unknown"


def _reconstruct_abstract(inv: Any) -> Optional[str]:
    """OpenAlex ships abstracts as {word: [positions]}."""
    if not isinstance(inv, dict) or not inv:
        return None
    words: dict[int, str] = {}
    for word, positions in inv.items():
        if isinstance(positions, list):
            for pos in positions:
                if type(pos) is int and 0 <= pos < 20000:
                    words[pos] = str(word)
    return " ".join(words[i] for i in sorted(words)) or None


def openalex_work(pid: str, no_cache: bool) -> dict:
    """OpenAlex record for doi:/pmid:/pmcid: as {title, year, venue, citedBy, ids, oaStatus,
    abstract, locations[]}. Keyless."""
    kind, _, value = pid.partition(":")
    if kind == "pmcid":
        value = value.upper().removeprefix("PMC")
    key = cache_key(pid, {"engine": "openalex"})
    hit = _cached(key, no_cache)
    if hit is not None:
        return hit
    try:
        resp = requests.get(f"{OPENALEX_ENDPOINT}/{kind}:{quote(value, safe='/')}", timeout=LOOKUP_TIMEOUT)
    except requests.RequestException as exc:
        raise PapersError(f"OpenAlex unreachable ({type(exc).__name__})") from None
    if resp.status_code == 404:
        raise PapersError("not found in OpenAlex either")
    if resp.status_code != 200:
        raise PapersError(f"OpenAlex error {resp.status_code}")
    try:
        w = resp.json()
    except ValueError:
        raise PapersError("OpenAlex returned non-JSON") from None
    if not isinstance(w, dict):
        raise PapersError("OpenAlex returned an unexpected shape")
    raw_ids = w.get("ids") if isinstance(w.get("ids"), dict) else {}
    ids: dict[str, str] = {}
    if isinstance(raw_ids.get("doi"), str):
        d = doi_in(raw_ids["doi"])
        if d:
            ids["doi"] = d
    if isinstance(raw_ids.get("pmid"), str):
        m = re.search(r"(\d+)$", raw_ids["pmid"])
        if m:
            ids["pmid"] = m.group(1)
    if isinstance(raw_ids.get("pmcid"), str):
        m = re.search(r"(\d+)$", raw_ids["pmcid"])
        if m:
            ids["pmcid"] = "PMC" + m.group(1)
    oa = w.get("open_access") if isinstance(w.get("open_access"), dict) else {}
    venue = None
    loc = w.get("primary_location") if isinstance(w.get("primary_location"), dict) else {}
    src = loc.get("source") if isinstance(loc.get("source"), dict) else {}
    if isinstance(src.get("display_name"), str):
        venue = src["display_name"]
    locations = []
    for loc in w.get("locations") or []:
        if not isinstance(loc, dict) or loc.get("is_oa") is not True:
            continue
        s = loc.get("source") if isinstance(loc.get("source"), dict) else {}
        for field in ("pdf_url", "landing_page_url"):
            u = loc.get(field)
            if isinstance(u, str) and u.startswith(("http://", "https://")):
                locations.append({"url": u, "pdf": field == "pdf_url",
                                  "sourceType": s.get("type") if isinstance(s.get("type"), str) else None,
                                  "version": loc.get("version") if isinstance(loc.get("version"), str) else None,
                                  "license": loc.get("license") if isinstance(loc.get("license"), str) else None})
    work = {
        "title": one_line(w.get("title")),
        "year": w.get("publication_year") if type(w.get("publication_year")) is int else None,
        "venue": venue,
        "citedBy": w.get("cited_by_count") if type(w.get("cited_by_count")) is int else None,
        "ids": ids,
        "oaStatus": oa.get("oa_status") if isinstance(oa.get("oa_status"), str) else None,
        "abstract": _reconstruct_abstract(w.get("abstract_inverted_index")),
        "locations": locations,
    }
    write_cache(CACHE_BUCKET, key, work)
    return work


def s2_open_pdf(pid: str) -> tuple[Optional[str], str]:
    """(open-access PDF URL or None, note). Semantic Scholar keyless often answers 429."""
    kind, _, value = pid.partition(":")
    s2_id = {"doi": "DOI:", "arxiv": "ARXIV:", "pmid": "PMID:", "pmcid": "PMCID:"}.get(kind)
    if not s2_id:
        return None, "no id Semantic Scholar takes"
    try:
        resp = requests.get(f"{S2_ENDPOINT}/{s2_id}{quote(value, safe='/')}",
                            params={"fields": "openAccessPdf"}, timeout=LOOKUP_TIMEOUT)
    except requests.RequestException as exc:
        return None, f"unreachable ({type(exc).__name__})"
    if resp.status_code == 429:
        return None, "rate limited (keyless), skipped"
    if resp.status_code == 404:
        return None, "not found"
    if resp.status_code != 200:
        return None, f"error {resp.status_code}"
    try:
        data = resp.json()
    except ValueError:
        return None, "non-JSON answer"
    oa = data.get("openAccessPdf") if isinstance(data, dict) and isinstance(data.get("openAccessPdf"), dict) else {}
    url = oa.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url, "open PDF"
    return None, "no open PDF"


def _add(cands: list[dict], url: str, kind: str, via: str, **extra: Any) -> None:
    if any(c["url"] == url for c in cands):
        return
    cands.append({"url": url, "kind": kind, "via": via, **{k: v for k, v in extra.items() if v}})


KIND_ORDER = {"publisher-oa": 0, "repository": 1, "author-site": 2, "social-upload": 3}


@papers.command("pdf")
@click.argument("paper_id", metavar="ID")
@click.option("--no-scholar", is_flag=True, help="Do not spend a Serper credit on a Scholar lookup")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--no-cache", is_flag=True, help="Bypass the local cache")
def pdf_cmd(paper_id: str, no_scholar: bool, json_output: bool, no_cache: bool):
    """Find a legal free copy of a paper (arXiv, OpenAlex, Semantic Scholar, trusted Scholar links).

    \b
    Only open-access sources: publisher OA pages, repositories (arXiv, PMC, ...), and
    authors' university pages. ResearchGate/Academia uploads are listed but flagged;
    links from unknown hosts are not shown. Then read it with:
        fcrawl scrape <url> --save-temp
    """
    pid = normalize_id(paper_id)
    kind = pid.partition(":")[0]
    cands: list[dict] = []
    notes: list[str] = []
    work: dict = {}
    hidden = 0
    scholar_credits = 0

    if kind == "arxiv":
        ax = pid.partition(":")[2]
        _add(cands, f"https://arxiv.org/pdf/{ax}", "repository", "arXiv", version="preprint")
    if kind in ("doi", "pmid", "pmcid"):
        try:
            work = openalex_work(pid, no_cache)
            notes.append(f"OpenAlex: {work.get('oaStatus') or 'unknown'}")
            for loc in sorted(work["locations"], key=lambda loc: not loc["pdf"]):
                k = "repository" if loc.get("sourceType") == "repository" else "publisher-oa"
                _add(cands, loc["url"], k, "OpenAlex", version=loc.get("version"), license=loc.get("license"),
                     pdf=loc["pdf"])
            if work["ids"].get("pmcid"):
                _add(cands, f"https://pmc.ncbi.nlm.nih.gov/articles/{work['ids']['pmcid']}/", "repository", "PMC")
        except PapersError as exc:
            notes.append(f"OpenAlex: {exc}")
    if kind == "pmcid":
        _add(cands, f"https://pmc.ncbi.nlm.nih.gov/articles/{pid.partition(':')[2]}/", "repository", "PMC")
    if not cands:
        url, note = s2_open_pdf(pid)
        notes.append(f"Semantic Scholar: {note}")
        if url:
            k = host_kind(url)
            _add(cands, url, k if k != "unknown" else "publisher-oa", "Semantic Scholar", pdf=True)
    title = work.get("title")
    if not cands and title and not no_scholar:
        try:
            items, scholar_credits, _ = scholar_search(title, SCHOLAR_PAGE, None, None, None, no_cache)
            want = title_norm(title)
            want_doi = work["ids"].get("doi")
            matched = 0
            for it in items:
                it_doi = ids_from_url(it.get("link")).get("doi")
                same_doi = bool(want_doi and it_doi and it_doi == want_doi)
                if not same_doi and title_norm(it.get("title")) != want:
                    continue
                matched += 1
                u = it.get("pdfUrl")
                if not isinstance(u, str):
                    continue
                k = host_kind(u)
                if k == "unknown":
                    hidden += 1  # 1 in 7 Scholar PDF links sits on an unknown, possibly pirate host
                    continue
                _add(cands, u, k, "Google Scholar", pdf=True)
            notes.append(f"Scholar: {matched} matching result(s)"
                         + (f", {hidden} PDF link(s) from unknown hosts not shown" if hidden else ""))
        except PapersError as exc:
            scholar_credits = exc.credits
            notes.append(f"Scholar: {exc}")
    elif not cands and not title and kind != "arxiv":
        notes.append("Scholar: skipped (no title to look up)")

    cands.sort(key=lambda c: (KIND_ORDER.get(c["kind"], 9), not c.get("pdf")))
    doi = work.get("ids", {}).get("doi") or (pid.partition(":")[2] if kind == "doi" else None)
    if scholar_credits:
        console.print(f"[dim]credits: {scholar_credits} Serper (Scholar lookup)[/dim]", highlight=False)
    if json_output:
        emit_json({"id": pid, "title": title, "found": bool(cands), "candidates": cands, "notes": notes,
                   "doiUrl": f"https://doi.org/{doi}" if doi else None, "scholarCredits": scholar_credits}, None)
        return
    head = title or pid
    if work.get("year"):
        head += f" ({work['year']})"
    out(f"{one_line(head)} · {pid}")
    if notes:
        console.print(f"[dim]{escape(one_line(' · '.join(notes)))}[/dim]", highlight=False)
    if cands:
        out("Free copies (best first):")
        for c in cands:
            extra = ", ".join(x for x in (c.get("version"), c.get("license")) if x)
            flag = "  ⚠ author upload, copyright unclear" if c["kind"] == "social-upload" else ""
            out(f"  [{c['kind']}] {one_line(c['url'])}" + (f"  ({extra}; via {c['via']})" if extra else f"  (via {c['via']})")
                  + flag)
        best = next((c for c in cands if c["kind"] != "social-upload"), None)
        out("---")
        if best:
            out(f"read it: fcrawl scrape {one_line(best['url'])} --save-temp")
        else:
            # only ResearchGate/Academia uploads: not a confirmed legal copy, so no
            # ready-made scrape line; the user decides
            out("Only an author upload with unclear copyright was found; ask the user before using it,")
            out("or get the paper through their own school or library access.")
        return
    out("No legal free copy found.")
    if doi:
        out(f"  Publisher page (usually abstract only): https://doi.org/{doi}")
    if institution.settings().get("enabled"):
        out(f"  Through your library (institution proxy is on): fcrawl papers fetch {pid}")
    else:
        out("  For the full text, download it with your own school or library access on your own computer.")


# --- fetch: the full text through the user's own library proxy ------------------------


def _fetch_target(paper_id: str) -> tuple[str, str, bool]:
    """(id or URL as given, publisher URL to fetch, needs_proxy). arXiv and PMC are free:
    fetched directly, not through the proxy and not counted."""
    raw = paper_id.strip()
    if raw.startswith(("http://", "https://")):
        host = _host(raw)
        free = _under(host, ("arxiv.org", "ncbi.nlm.nih.gov"))
        return raw, raw, not free
    pid = normalize_id(raw)
    kind, _, value = pid.partition(":")
    if kind == "arxiv":
        return pid, f"https://arxiv.org/pdf/{value}", False
    if kind == "pmcid":
        return pid, f"https://pmc.ncbi.nlm.nih.gov/articles/{value}/pdf/", False
    if kind == "doi":
        return pid, f"https://doi.org/{value}", True
    raise click.BadParameter(f"{raw}: give a DOI, an arXiv/PMC id or the paper's URL", param_hint="ID")


def _fetch_filename(label: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", label.split("://", 1)[-1]).strip("-.")[:120]
    return (safe or "paper") + ".pdf"


@papers.command("fetch")
@click.argument("paper_id", metavar="ID_OR_URL")
@click.option("-o", "--output", "output", default=None, type=click.Path(dir_okay=False),
              help="Where to save the PDF (default: the fcrawl-saved/papers temp folder)")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def fetch_cmd(paper_id: str, output: Optional[str], json_output: bool):
    """Download a paper's PDF through your own library proxy (fcrawl auth), to a local file.

    \b
    Needs `fcrawl auth enable <preset>` and a session from `fcrawl auth login`.
    The request goes from this machine to the library proxy only, never through the
    gateway or Firecrawl. Built-in limits: 20 papers a day, 10 per publisher, 30 s apart.
    arXiv / PMC ids are free and are fetched directly.
    """
    from pathlib import Path

    from ..utils.platform import tmp_root

    label, target, needs_proxy = _fetch_target(paper_id)
    out_path = Path(output) if output else tmp_root() / "fcrawl-saved" / "papers" / _fetch_filename(label)
    try:
        if needs_proxy:
            preset = institution.active_preset()
            store = institution.CookieStore.load(preset.id)
            fetched = institution.fetch_pdf(preset, store, target, out_path,
                                            note=lambda m: console.print(f"[dim]{escape(m)}[/dim]", highlight=False))
            provider = f"institution:{preset.id}"
        else:
            fetched = institution.fetch_free_pdf(target, out_path)
            provider = "direct"
    except institution.InstitutionError as exc:
        console.print(f"[red]Error: {escape(str(exc))}[/red]", highlight=False)
        if json_output:
            emit_json({"id": label, "ok": False, "error": str(exc)}, None)
        else:
            out(f"RESULT provider={'institution' if needs_proxy else 'direct'} url={target} status=failed")
        raise click.Abort()
    except requests.RequestException as exc:
        console.print(f"[red]Error: {type(exc).__name__} while fetching {escape(target)}[/red]", highlight=False)
        raise click.Abort()
    if json_output:
        emit_json({"id": label, "ok": True, "path": str(fetched.path), "bytes": fetched.size, "url": target,
                   "pdfUrl": fetched.pdf_url, "provider": provider}, None)
        return
    out(f"RESULT path={fetched.path} provider={provider} url={target} status=ok bytes={fetched.size}")

