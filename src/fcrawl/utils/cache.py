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


def search_result_to_dict(result) -> dict:
    """Convert Firecrawl search result to cacheable dict"""
    data = {}

    def item_to_dict(item):
        d = {}
        for attr in ['url', 'title', 'description', 'markdown', 'html', 'links',
                     'engine', 'engines', 'score']:
            if hasattr(item, attr):
                d[attr] = getattr(item, attr)
        if hasattr(item, 'metadata') and item.metadata:
            d['metadata'] = item.metadata.__dict__ if hasattr(item.metadata, '__dict__') else item.metadata
        return d

    if hasattr(result, 'web') and result.web:
        data['web'] = [item_to_dict(i) for i in result.web]
    if hasattr(result, 'news') and result.news:
        data['news'] = [item_to_dict(i) for i in result.news]
    if hasattr(result, 'images') and result.images:
        data['images'] = [item_to_dict(i) for i in result.images]

    return data


class CachedSearchItem:
    """Wrapper for cached search result item"""
    def __init__(self, data: dict):
        for key, value in data.items():
            if key == 'metadata' and isinstance(value, dict):
                setattr(self, key, CachedMetadata(value))
            else:
                setattr(self, key, value)


class CachedSearchResult:
    """Wrapper to make cached search dict behave like Firecrawl search result"""
    def __init__(self, data: dict):
        self.web = [CachedSearchItem(i) for i in data.get('web', [])] if 'web' in data else None
        self.news = [CachedSearchItem(i) for i in data.get('news', [])] if 'news' in data else None
        self.images = [CachedSearchItem(i) for i in data.get('images', [])] if 'images' in data else None


def crawl_result_to_dict(result) -> dict:
    """Convert Firecrawl crawl result to cacheable dict"""
    data = {'pages': []}

    if hasattr(result, 'data') and result.data:
        for page in result.data:
            page_data = {}
            if hasattr(page, 'markdown'):
                page_data['markdown'] = page.markdown
            if hasattr(page, 'html'):
                page_data['html'] = page.html
            if hasattr(page, 'links'):
                page_data['links'] = page.links
            if hasattr(page, 'metadata') and page.metadata:
                page_data['metadata'] = page.metadata.__dict__ if hasattr(page.metadata, '__dict__') else page.metadata
            data['pages'].append(page_data)

    return data


class CachedCrawlPage:
    """Wrapper for cached crawl page"""
    def __init__(self, data: dict):
        self.markdown = data.get('markdown')
        self.html = data.get('html')
        self.links = data.get('links')
        if 'metadata' in data:
            self.metadata = CachedMetadata(data['metadata'])
        else:
            self.metadata = None


class CachedCrawlResult:
    """Wrapper to make cached crawl dict behave like Firecrawl crawl result"""
    def __init__(self, data: dict):
        self.data = [CachedCrawlPage(p) for p in data.get('pages', [])]
