"""Result aggregation and deduplication for multi-engine search"""

from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from typing import Optional

from .base import SearchResult


def normalize_url(url: str) -> str:
    """
    Normalize URL for deduplication.

    - Removes www. prefix
    - Removes trailing slashes
    - Sorts query parameters
    - Removes common tracking params (utm_*, ref, etc.)
    - Lowercases scheme and host
    """
    try:
        parsed = urlparse(url)

        # Lowercase scheme and host
        scheme = parsed.scheme.lower()
        netloc = parsed.netloc.lower()

        # Remove www. prefix
        if netloc.startswith("www."):
            netloc = netloc[4:]

        # Remove trailing slash from path
        path = parsed.path.rstrip("/")

        # Parse and clean query parameters
        query_params = parse_qs(parsed.query, keep_blank_values=True)

        # Remove tracking parameters
        tracking_params = {
            "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
            "ref", "source", "campaign", "fbclid", "gclid", "msclkid",
            "mc_cid", "mc_eid", "dclid", "srsltid",
        }
        cleaned_params = {
            k: v for k, v in query_params.items()
            if k.lower() not in tracking_params
        }

        # Sort and rebuild query string
        if cleaned_params:
            # Flatten single-value lists
            flat_params = {
                k: v[0] if len(v) == 1 else v
                for k, v in sorted(cleaned_params.items())
            }
            query = urlencode(flat_params, doseq=True)
        else:
            query = ""

        # Rebuild URL without fragment
        return urlunparse((scheme, netloc, path, "", query, ""))

    except Exception:
        # If parsing fails, return original
        return url


def aggregate_results(
    all_results: list[SearchResult],
    limit: Optional[int] = None
) -> list[dict]:
    """
    Aggregate and deduplicate results from multiple engines.

    Scoring:
    - Found by N engines = score N
    - Higher position in original results = bonus

    Returns results sorted by:
    1. Number of engines that found it (desc)
    2. Best position across engines (asc)
    """
    by_url: dict[str, dict] = {}

    for result in all_results:
        normalized = normalize_url(result.url)

        if normalized in by_url:
            # Same URL found by another engine
            existing = by_url[normalized]
            existing["engines"].append(result.engine)
            existing["score"] += 1
            # Track best position
            if result.position < existing["best_position"]:
                existing["best_position"] = result.position
        else:
            by_url[normalized] = {
                "url": result.url,
                "title": result.title,
                "description": result.description,
                "engine": result.engine,  # Primary engine (first to find)
                "engines": [result.engine],
                "score": 1,
                "position": result.position,
                "best_position": result.position,
            }

    # Sort by: score DESC, then best_position ASC
    ranked = sorted(
        by_url.values(),
        key=lambda x: (-x["score"], x["best_position"])
    )

    # Apply limit if specified
    if limit:
        ranked = ranked[:limit]

    return ranked


def format_engines_badge(engines: list[str]) -> str:
    """Format engines list as a badge string"""
    return f"({', '.join(sorted(set(engines)))})"


def get_aggregation_stats(results: list[dict]) -> dict:
    """Get statistics about aggregated results"""
    total = len(results)

    # Count by number of engines
    by_engine_count = {}
    for r in results:
        count = r["score"]
        by_engine_count[count] = by_engine_count.get(count, 0) + 1

    # Count by primary engine
    by_engine = {}
    for r in results:
        engine = r["engine"]
        by_engine[engine] = by_engine.get(engine, 0) + 1

    return {
        "total": total,
        "by_engine_count": by_engine_count,
        "by_primary_engine": by_engine,
    }
