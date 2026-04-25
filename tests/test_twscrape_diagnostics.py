from __future__ import annotations

import asyncio

import httpx
import pytest

from fcrawl.commands._x_diagnose import check_xclid
from fcrawl.vendors.twscrape.accounts_pool import AccountsPool, NoAccountError
from fcrawl.vendors.twscrape.queue_client import classify_error, response_body_snippet
from fcrawl.vendors.twscrape.utils import utc


def run(coro):
    return asyncio.run(coro)


def test_no_account_error_distinguishes_empty_pool(tmp_path):
    async def scenario():
        pool = AccountsPool(db_file=str(tmp_path / "accounts.db"), raise_when_no_account=True)
        with pytest.raises(NoAccountError) as exc:
            await pool.get_for_queue_or_wait("TweetDetail")
        assert exc.value.cause == "no_accounts_added"

    run(scenario())


def test_no_account_error_distinguishes_unknown_locks(tmp_path):
    async def scenario():
        pool = AccountsPool(db_file=str(tmp_path / "accounts.db"), raise_when_no_account=True)
        await pool.add_account_from_tokens("one", "ct0", "auth")
        await pool.lock_until(
            "one",
            "TweetDetail",
            utc.ts() + 3600,
            cause="unknown",
            error_status=403,
            error_body="client transaction id rejected",
            error_msg="OK",
        )

        with pytest.raises(NoAccountError) as exc:
            await pool.get_for_queue_or_wait("TweetDetail")

        assert exc.value.cause == "all_unknown_errors"
        assert exc.value.next_unlock_at is not None

        info = await pool.accounts_info()
        assert info[0]["last_error_cause"] == "unknown"
        assert info[0]["last_error_status"] == 403
        assert info[0]["last_error_body"] == "client transaction id rejected"

    run(scenario())


def test_no_account_error_distinguishes_rate_limited_accounts(tmp_path):
    async def scenario():
        pool = AccountsPool(db_file=str(tmp_path / "accounts.db"), raise_when_no_account=True)
        await pool.add_account_from_tokens("one", "ct0", "auth")
        await pool.lock_until(
            "one",
            "SearchTimeline",
            utc.ts() + 3600,
            cause="rate_limit",
            error_status=429,
            error_body='{"errors":[{"code":88,"message":"Rate limit exceeded"}]}',
            error_msg="(88) Rate limit exceeded",
        )

        with pytest.raises(NoAccountError) as exc:
            await pool.get_for_queue_or_wait("SearchTimeline")

        assert exc.value.cause == "all_rate_limited"

    run(scenario())


def test_no_account_error_distinguishes_inactive_auth_failures(tmp_path):
    async def scenario():
        pool = AccountsPool(db_file=str(tmp_path / "accounts.db"), raise_when_no_account=True)
        await pool.add_account_from_tokens("one", "ct0", "auth")
        await pool.mark_inactive(
            "one",
            "(32) Could not authenticate you",
            cause="auth_expired",
            error_status=401,
            error_body='{"errors":[{"code":32}]}',
        )

        with pytest.raises(NoAccountError) as exc:
            await pool.get_for_queue_or_wait("TweetDetail")

        assert exc.value.cause == "all_banned"

    run(scenario())


def test_response_body_snippet_compacts_and_limits_body():
    rep = httpx.Response(
        403,
        request=httpx.Request("GET", "https://x.com/i/api/graphql/test"),
        text="first\n\nsecond " + ("x" * 600),
    )
    snippet = response_body_snippet(rep)
    assert "\n" not in snippet
    assert snippet.startswith("first second")
    assert len(snippet) == 500


def test_classify_error_labels_auth_and_unknown():
    req = httpx.Request("GET", "https://x.com")
    assert classify_error(httpx.Response(429, request=req), "OK", 0) == "rate_limit"
    assert classify_error(httpx.Response(403, request=req), "OK", -1) == "auth_expired"
    assert (
        classify_error(
            httpx.Response(403, request=req),
            "(326) Authorization: Denied by access control",
            10,
        )
        == "banned"
    )
    assert classify_error(httpx.Response(400, request=req), "OK", -1) == "unknown"


def test_xclid_diagnose_reports_parser_snippet():
    async def broken_home():
        return 'window.__chunks__=e=>(({abc:"main"})[e]||e)+"."+({broken:})[e]+"a.js"'

    result = run(check_xclid(fetch=broken_home))
    assert result["ok"] is False
    assert "snippet" in result
    assert "a.js" in result["snippet"]
