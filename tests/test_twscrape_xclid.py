from __future__ import annotations

import asyncio

import httpx
import pytest

import fcrawl.vendors.twscrape.xclid as xclid
from fcrawl.vendors.twscrape.account import Account, make_x_http_client
from fcrawl.vendors.twscrape.accounts_pool import AccountsPool
from fcrawl.vendors.twscrape.queue_client import Ctx, QueueClient, XClIdGenStore


def run(coro):
    return asyncio.run(coro)


def make_account(*, proxy: str | None = None) -> Account:
    return Account(
        username="one",
        password="password",
        email="one@example.com",
        email_password="email-password",
        user_agent="account-agent",
        active=True,
        cookies={"auth_token": "secret-auth", "ct0": "secret-csrf"},
        proxy=proxy,
    )


def test_account_cookies_are_scoped_to_x_and_stripped_on_redirect():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("cookie")))
        if request.url == httpx.URL("https://x.com/start"):
            return httpx.Response(
                302,
                headers={"location": "https://redirect.example/chunk.js"},
                request=request,
            )
        return httpx.Response(200, request=request)

    async def scenario():
        async with make_x_http_client(
            cookies={"auth_token": "secret-auth", "ct0": "secret-csrf"},
            transport=httpx.MockTransport(handler),
        ) as client:
            await client.get("https://x.com/start")
            await client.get("https://abs.twimg.com/x-web/chunk.js")

    run(scenario())

    assert seen == [
        (
            "https://x.com/start",
            "auth_token=secret-auth; ct0=secret-csrf",
        ),
        ("https://redirect.example/chunk.js", None),
        ("https://abs.twimg.com/x-web/chunk.js", None),
    ]


def test_account_resolves_proxy_precedence(monkeypatch):
    account = make_account(proxy="http://account-proxy")

    monkeypatch.delenv("TWS_PROXY", raising=False)
    assert account.resolve_proxy() == "http://account-proxy"

    monkeypatch.setenv("TWS_PROXY", "http://env-proxy")
    assert account.resolve_proxy() == "http://env-proxy"
    assert account.resolve_proxy("http://api-proxy") == "http://api-proxy"


def test_xclid_uses_separate_cookieless_asset_client(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self):
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.closed = True

    def fake_make_client(proxy=None, cookies=None, user_agent=None):
        client = FakeClient()
        created.append(
            {
                "proxy": proxy,
                "cookies": cookies,
                "user_agent": user_agent,
                "client": client,
            }
        )
        return client

    async def fake_page(url, clt):
        assert clt is created[0]["client"]
        return "<html></html>"

    async def fake_load_keys(soup, clt):
        assert clt is created[1]["client"]
        return [1, 2, 3], "animation-key"

    monkeypatch.setattr(xclid, "_make_client", fake_make_client)
    monkeypatch.setattr(xclid, "get_tw_page_text", fake_page)
    monkeypatch.setattr(xclid, "load_keys", fake_load_keys)

    gen = run(
        xclid.XClIdGen.create(
            proxy="http://effective-proxy",
            cookies={"auth_token": "auth", "ct0": "csrf"},
            user_agent="account-agent",
        )
    )

    assert gen.anim_key == "animation-key"
    assert [item["cookies"] for item in created] == [
        {"auth_token": "auth", "ct0": "csrf"},
        None,
    ]
    assert {item["proxy"] for item in created} == {"http://effective-proxy"}
    assert {item["user_agent"] for item in created} == {"account-agent"}
    assert all(item["client"].closed for item in created)


def test_queue_context_uses_effective_environment_proxy(monkeypatch):
    account = make_account(proxy="http://account-proxy")
    seen = {}

    class FakePool:
        async def get_for_queue_or_wait(self, queue):
            return account

    def fake_make_client(self, proxy=None):
        seen["proxy"] = proxy
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200)))

    async def scenario():
        client = QueueClient(FakePool(), "SearchTimeline")
        ctx = await client._get_ctx()
        assert ctx.proxy == "http://env-proxy"
        await ctx.aclose()

    monkeypatch.setenv("TWS_PROXY", "http://env-proxy")
    monkeypatch.setattr(Account, "make_client", fake_make_client)
    run(scenario())

    assert seen["proxy"] == "http://env-proxy"


def test_ctx_passes_effective_context_to_xclid_store(monkeypatch):
    account = make_account()
    seen = {}

    class FakeGen:
        def calc(self, method, path):
            return "transaction-id"

    async def fake_get(cls, username, **kwargs):
        seen.update(username=username, **kwargs)
        return FakeGen()

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            )
        )
        ctx = Ctx(account, client, proxy="http://effective-proxy")
        try:
            await ctx.req("GET", "https://x.com/i/api/graphql/test")
        finally:
            await ctx.aclose()

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(fake_get))
    run(scenario())

    assert seen == {
        "username": "one",
        "fresh": False,
        "proxy": "http://effective-proxy",
        "cookies": {"auth_token": "secret-auth", "ct0": "secret-csrf"},
        "user_agent": "account-agent",
    }


def test_global_xclid_parse_failure_does_not_lock_account(tmp_path, monkeypatch):
    async def fail_parse(cls, username, **kwargs):
        raise xclid.XClIdParseError("new X bundle format")

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(fail_parse))

    async def scenario():
        pool = AccountsPool(
            db_file=str(tmp_path / "accounts.db"),
            raise_when_no_account=True,
        )
        await pool.add_account_from_tokens("one", "csrf", "auth")

        client = QueueClient(pool, "SearchTimeline")
        assert await client.get("https://x.com/i/api/graphql/test") is None

        account = await pool.get("one")
        assert account.active is True
        assert "SearchTimeline" not in account.locks

    run(scenario())


def test_account_specific_xclid_failure_rotates_without_disabling(tmp_path, monkeypatch):
    class FakeGen:
        def calc(self, method, path):
            return "transaction-id"

    async def account_sensitive_get(cls, username, **kwargs):
        if username == "a_bad":
            raise xclid.XClIdAccountError("logged-out web entry")
        return FakeGen()

    def fake_make_client(self, proxy=None):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"ok": True}, request=request)
            )
        )

    monkeypatch.setattr(XClIdGenStore, "get", classmethod(account_sensitive_get))
    monkeypatch.setattr(Account, "make_client", fake_make_client)

    async def scenario():
        pool = AccountsPool(
            db_file=str(tmp_path / "accounts.db"),
            raise_when_no_account=True,
        )
        await pool.add_account_from_tokens("a_bad", "csrf-a", "auth-a")
        await pool.add_account_from_tokens("b_good", "csrf-b", "auth-b")

        client = QueueClient(pool, "SearchTimeline")
        rep = await client.get("https://x.com/i/api/graphql/test")
        assert rep is not None
        assert getattr(rep, "__username") == "b_good"

        bad = await pool.get("a_bad")
        assert bad.active is True
        assert "SearchTimeline" in bad.locks
        await client.__aexit__(None, None, None)

    run(scenario())


def test_logged_out_entry_is_an_account_error():
    html = (
        '<script src="https://abs.twimg.com/x-web/client-web/'
        'entry-client-logged-out-a1b2c3.js"></script>'
    )
    with pytest.raises(xclid.XClIdAccountError, match="logged-out web entry"):
        xclid.get_scripts_list(html)


def test_complete_asset_scan_is_parse_error():
    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="no signing reference")
            )
        )
        try:
            with pytest.raises(
                xclid.XClIdParseError,
                match=r"2 loaded, 0 failed",
            ):
                await xclid._find_indices_url(
                    [
                        "https://abs.twimg.com/x-web/a.js",
                        "https://abs.twimg.com/x-web/b.js",
                    ],
                    client,
                )
        finally:
            await client.aclose()

    run(scenario())


def test_incomplete_asset_scan_is_transport_error():
    def handler(request: httpx.Request) -> httpx.Response:
        status = 503 if request.url.path.endswith("/a.js") else 200
        return httpx.Response(status, text="no signing reference", request=request)

    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(
                xclid.XClIdTransportError,
                match=r"1 loaded, 1 failed",
            ):
                await xclid._find_indices_url(
                    [
                        "https://abs.twimg.com/x-web/a.js",
                        "https://abs.twimg.com/x-web/b.js",
                    ],
                    client,
                )
        finally:
            await client.aclose()

    run(scenario())
