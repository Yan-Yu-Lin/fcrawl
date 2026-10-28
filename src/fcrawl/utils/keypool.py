"""Firecrawl API key pool shared across fcrawl processes.

Rate limits are per Firecrawl team (account), so each key here should come
from a different account. Processes coordinate through a small JSON state
file under /tmp guarded by flock: each key tracks the timestamps of requests
sent in the last 60 s, a cooldown deadline set from 429 Retry-After, and a
disabled flag for 401/402.
"""

import fcntl
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple, TypeVar

STATE_DIR = Path("/tmp/fcrawl-keys")
WINDOW_SECONDS = 60
DEFAULT_RETRY_AFTER = 10
DEFAULT_MAX_WAIT = 75

T = TypeVar("T")


class KeyPoolExhausted(RuntimeError):
    """All keys are rate limited (and waiting would exceed max_wait) or disabled."""


def key_id(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def key_label(key: str) -> str:
    return f"…{key[-4:]}"


def _log(message: str) -> None:
    print(f"[keypool] {message}", file=sys.stderr)


def parse_retry_after(exc: Exception) -> int:
    """Seconds to wait from a 429 error: header first, then message text."""
    response = getattr(exc, "response", None)
    header = getattr(response, "headers", {}).get("Retry-After") if response is not None else None
    if header and str(header).isdigit():
        return int(header)
    match = re.search(r"retry after (\d+)\s*s", str(exc), re.IGNORECASE)
    if match:
        return int(match.group(1))
    return DEFAULT_RETRY_AFTER


class KeyPool:
    def __init__(
        self,
        keys: List[str],
        rate_limit: int = 10,
        max_wait: int = DEFAULT_MAX_WAIT,
        state_dir: Path = STATE_DIR,
    ):
        if not keys:
            raise KeyPoolExhausted("No Firecrawl API key configured")
        self.keys = list(dict.fromkeys(keys))
        self.rate_limit = rate_limit
        self.max_wait = max_wait
        self.state_dir = state_dir
        self.state_file = state_dir / "state.json"
        self.lock_file = state_dir / "lock"
        self.waited = 0.0  # seconds spent sleeping in the last call()

    # --- state file -------------------------------------------------------

    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, state: dict) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, self.state_file)

    def _entry(self, state: dict, key: str) -> dict:
        return state.setdefault(
            key_id(key),
            {"label": key_label(key), "recent": [], "cooldown_until": 0, "disabled": None},
        )

    def _locked(self, fn: Callable[[dict], T]) -> T:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_file, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self._load()
            now = time.time()
            for entry in state.values():
                entry["recent"] = [t for t in entry.get("recent", []) if now - t < WINDOW_SECONDS]
            result = fn(state)
            self._save(state)
            return result

    # --- selection --------------------------------------------------------

    def pick(self) -> Tuple[Optional[str], float]:
        """Return (key, seconds_to_wait). key is None when nothing is usable yet.

        When seconds_to_wait is 0 the request timestamp has already been
        recorded, so the caller must send the request.
        """

        def choose(state: dict):
            now = time.time()
            usable = []
            for key in self.keys:
                entry = self._entry(state, key)
                if entry["disabled"]:
                    continue
                usable.append((key, entry))
            if not usable:
                raise KeyPoolExhausted("All Firecrawl API keys are disabled (401/402)")

            ready = [(k, e) for k, e in usable if e["cooldown_until"] <= now]
            if not ready:
                soonest = min(e["cooldown_until"] for _, e in usable)
                return None, soonest - now

            key, entry = min(
                ready, key=lambda ke: (len(ke[1]["recent"]), ke[1]["recent"][-1] if ke[1]["recent"] else 0)
            )
            if len(entry["recent"]) >= self.rate_limit:
                return None, entry["recent"][0] + WINDOW_SECONDS - now
            entry["recent"].append(now)
            return key, 0.0

        return self._locked(choose)

    def mark_rate_limited(self, key: str, retry_after: int) -> None:
        def update(state: dict):
            self._entry(state, key)["cooldown_until"] = time.time() + retry_after

        self._locked(update)

    def mark_disabled(self, key: str, reason: str) -> None:
        def update(state: dict):
            self._entry(state, key)["disabled"] = reason

        self._locked(update)

    # --- rotation ---------------------------------------------------------

    def call(self, fn: Callable[[str], T]) -> T:
        """Run fn(api_key), rotating keys on 429 and disabling on 401/402.

        Waits (up to max_wait total) when every key is cooling down or has
        used up its per-minute budget.
        """
        from firecrawl.v2.utils.error_handler import FirecrawlError

        self.waited = 0.0
        while True:
            key, wait = self.pick()
            if key is None:
                wait = max(wait, 0.5)
                if self.waited + wait > self.max_wait:
                    raise KeyPoolExhausted(
                        f"All Firecrawl API keys rate limited; next free in {wait:.0f}s "
                        f"(exceeds --max-wait {self.max_wait}s)"
                    )
                _log(f"all keys rate limited, waiting {wait:.0f}s…")
                time.sleep(wait)
                self.waited += wait
                continue

            try:
                return fn(key)
            except FirecrawlError as exc:
                code = exc.status_code
                if code == 429:
                    retry_after = parse_retry_after(exc)
                    self.mark_rate_limited(key, retry_after)
                    _log(f"key {key_label(key)} rate limited for {retry_after}s, rotating")
                    continue
                if code in (401, 402):
                    self.mark_disabled(key, str(code))
                    _log(f"key {key_label(key)} disabled ({code}), rotating")
                    continue
                raise


def reset_state(state_dir: Path = STATE_DIR) -> None:
    try:
        (state_dir / "state.json").unlink()
    except FileNotFoundError:
        pass
