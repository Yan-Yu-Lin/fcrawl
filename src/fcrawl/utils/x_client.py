"""X/Twitter client utilities for fcrawl"""

import os
from pathlib import Path

from ..vendors.twscrape import API, AccountsPool


def get_x_config_dir() -> Path:
    """Return the fcrawl config directory, creating it if needed."""
    config_dir = Path.home() / ".config" / "fcrawl"
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def get_x_db_path() -> str:
    """Return the path to the X accounts database."""
    return str(get_x_config_dir() / "x_accounts.db")


def get_x_api(raise_when_no_account: bool = True) -> API:
    """Return a configured twscrape API instance.

    Args:
        raise_when_no_account: If True, raise NoAccountError when no accounts available.
                               If False, wait for accounts to become available.

    Returns:
        Configured API instance pointing to the fcrawl accounts database.
    """
    db_path = get_x_db_path()
    return API(pool=db_path, raise_when_no_account=raise_when_no_account)


def get_x_pool() -> AccountsPool:
    """Return an AccountsPool instance for direct account management."""
    db_path = get_x_db_path()
    return AccountsPool(db_file=db_path)
