"""Account model for twscrape"""

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime

import httpx
from httpx import AsyncClient, AsyncHTTPTransport

from .models import JSONTrait
from .utils import utc

TOKEN = "Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
X_COOKIE_DOMAIN = ".x.com"


def has_required_cookies(cookies: dict[str, str]) -> bool:
    return all(cookies.get(name) for name in ("auth_token", "ct0"))


def make_x_http_client(
    *,
    proxy: str | None = None,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float | httpx.Timeout | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncClient:
    """Build an X client without turning account cookies into cross-site cookies."""
    client = AsyncClient(
        proxy=proxy,
        follow_redirects=True,
        transport=transport or AsyncHTTPTransport(retries=3),
        headers=headers,
        **({"timeout": timeout} if timeout is not None else {}),
    )
    for name, value in (cookies or {}).items():
        client.cookies.set(name, value, domain=X_COOKIE_DOMAIN, path="/")
    return client


@dataclass
class Account(JSONTrait):
    username: str
    password: str
    email: str
    email_password: str
    user_agent: str
    active: bool
    locks: dict[str, datetime] = field(default_factory=dict)  # queue: datetime
    stats: dict[str, int] = field(default_factory=dict)  # queue: requests
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    mfa_code: str | None = None
    proxy: str | None = None
    error_msg: str | None = None
    last_error_cause: str | None = None
    last_error_status: int | None = None
    last_error_body: str | None = None
    last_used: datetime | None = None
    _tx: str | None = None

    @staticmethod
    def from_rs(rs: sqlite3.Row):
        doc = dict(rs)
        doc["locks"] = {k: utc.from_iso(v) for k, v in json.loads(doc["locks"]).items()}
        doc["stats"] = {k: v for k, v in json.loads(doc["stats"]).items() if isinstance(v, int)}
        doc["headers"] = json.loads(doc["headers"])
        doc["cookies"] = json.loads(doc["cookies"])
        doc["active"] = bool(doc["active"])
        doc["last_used"] = utc.from_iso(doc["last_used"]) if doc["last_used"] else None
        return Account(**doc)

    def to_rs(self):
        rs = asdict(self)
        rs["locks"] = json.dumps(rs["locks"], default=lambda x: x.isoformat())
        rs["stats"] = json.dumps(rs["stats"])
        rs["headers"] = json.dumps(rs["headers"])
        rs["cookies"] = json.dumps(rs["cookies"])
        rs["last_used"] = rs["last_used"].isoformat() if rs["last_used"] else None
        return rs

    def resolve_proxy(self, proxy: str | None = None) -> str | None:
        proxies = [proxy, os.getenv("TWS_PROXY"), self.proxy]
        proxies = [x for x in proxies if x is not None]
        return proxies[0] if proxies else None

    def make_client(self, proxy: str | None = None) -> AsyncClient:
        headers = {
            **self.headers,
            "user-agent": self.user_agent,
            "content-type": "application/json",
            "authorization": TOKEN,
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
        }

        if "ct0" in self.cookies:
            headers["x-csrf-token"] = self.cookies["ct0"]

        return make_x_http_client(
            proxy=self.resolve_proxy(proxy),
            cookies=self.cookies,
            headers=headers,
        )
