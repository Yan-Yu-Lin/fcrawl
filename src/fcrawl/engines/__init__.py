"""Search engines module for csearch command"""

from .base import SearchEngine, SearchResult
from .google import GoogleEngine
from .bing import BingEngine
from .brave import BraveEngine
from .aggregator import aggregate_results, normalize_url

__all__ = [
    'SearchEngine',
    'SearchResult',
    'GoogleEngine',
    'BingEngine',
    'BraveEngine',
    'aggregate_results',
    'normalize_url',
]

# Engine registry for easy lookup
ENGINES = {
    'google': GoogleEngine,
    'bing': BingEngine,
    'brave': BraveEngine,
}


def get_engine(name: str) -> type:
    """Get engine class by name"""
    name = name.lower()
    if name not in ENGINES:
        raise ValueError(f"Unknown engine: {name}. Available: {', '.join(ENGINES.keys())}")
    return ENGINES[name]


def get_all_engines() -> list[str]:
    """Get list of all available engine names"""
    return list(ENGINES.keys())
