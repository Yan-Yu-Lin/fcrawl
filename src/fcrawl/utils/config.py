"""Configuration management for fcrawl"""

import os
import json
from pathlib import Path
from typing import Dict, Any, Optional
from firecrawl import Firecrawl
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

DEFAULT_CONFIG = {
    "api_url": "http://localhost:3002",
    "api_key": None,
    "serper_api_key": None,
    "default_format": "markdown",
    "cache_enabled": False,
    "cache_duration": 3600,  # 1 hour in seconds
}


def get_config_path() -> Path:
    """Get the configuration file path"""
    # Check for local config first
    local_config = Path(".fcrawlrc")
    if local_config.exists():
        return local_config

    # Then check home directory
    home_config = Path.home() / ".fcrawlrc"
    return home_config


def load_config() -> Dict[str, Any]:
    """Load configuration from file and environment"""
    config = DEFAULT_CONFIG.copy()

    # Load from config file if it exists
    config_path = get_config_path()
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                file_config = json.load(f)
                config.update(file_config)
        except Exception:
            pass  # Use defaults if config is invalid

    # Override with environment variables
    if os.getenv("FIRECRAWL_API_URL"):
        config["api_url"] = os.getenv("FIRECRAWL_API_URL")
    if os.getenv("FIRECRAWL_API_KEY"):
        config["api_key"] = os.getenv("FIRECRAWL_API_KEY")

    return config


def save_config(config: Dict[str, Any], path: Optional[Path] = None):
    """Save configuration to file"""
    config_path = path or get_config_path()
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def get_firecrawl_client() -> Firecrawl:
    """Get a configured Firecrawl client"""
    config = load_config()

    # For local instances, use a dummy API key if none is set
    api_key = config.get("api_key")
    if not api_key and "localhost" in config["api_url"]:
        api_key = "local-dummy-key"

    client_args = {
        "api_url": config["api_url"],
        "api_key": api_key or "dummy-key",  # SDK requires some value
    }

    return Firecrawl(**client_args)
