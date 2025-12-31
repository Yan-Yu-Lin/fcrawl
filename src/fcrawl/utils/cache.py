"""Cache utilities for fcrawl"""

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Optional

CACHE_DIR = Path("/tmp/fcrawl-cache")


def cache_key(identifier: str, options: dict = None) -> str:
    """Generate cache key from identifier (URL or query) and options"""
    key = identifier
    if options:
        key += json.dumps(options, sort_keys=True)
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def get_cache_path(command: str, key: str) -> Path:
    """Get cache file path for a command"""
    return CACHE_DIR / command / f"{key}.json"


def read_cache(command: str, key: str) -> Optional[dict]:
    """Read from cache if exists"""
    path = get_cache_path(command, key)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, IOError):
            return None
    return None


def write_cache(command: str, key: str, data: dict):
    """Write to cache"""
    path = get_cache_path(command, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def clear_cache(command: str = None):
    """Clear cache for a command or all commands"""
    if command:
        path = CACHE_DIR / command
        if path.exists():
            shutil.rmtree(path)
    else:
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)


def result_to_dict(result) -> dict:
    """Convert Firecrawl result object to cacheable dict"""
    data = {}
    if hasattr(result, 'markdown'):
        data['markdown'] = result.markdown
    if hasattr(result, 'html'):
        data['html'] = result.html
    if hasattr(result, 'links'):
        data['links'] = result.links
    if hasattr(result, 'metadata') and result.metadata:
        md = result.metadata
        data['metadata'] = md.__dict__ if hasattr(md, '__dict__') else md
    return data


class CachedResult:
    """Wrapper to make cached dict behave like Firecrawl result object"""
    def __init__(self, data: dict):
        self._data = data
        self.markdown = data.get('markdown')
        self.html = data.get('html')
        self.links = data.get('links')
        if 'metadata' in data:
            self.metadata = CachedMetadata(data['metadata'])
        else:
            self.metadata = None


class CachedMetadata:
    """Wrapper for cached metadata"""
    def __init__(self, data: dict):
        for key, value in data.items():
            setattr(self, key, value)
