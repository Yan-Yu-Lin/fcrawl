from __future__ import annotations

import asyncio

import httpx
import pytest

from fcrawl.commands._x_diagnose import check_xclid
from twscrape.accounts_pool import AccountsPool, NoAccountError
from twscrape.queue_client import response_body_snippet
from twscrape.utils import utc


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
        await pool.add_account_cookies("one", "ct0=ct0; auth_token=auth")
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
        await pool.add_account_cookies("one", "ct0=ct0; auth_token=auth")
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
        await pool.add_account_cookies("one", "ct0=ct0; auth_token=auth")
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
    from twscrape.http import Response

    rep = Response(httpx.Response(
        403,
        request=httpx.Request("GET", "https://x.com/i/api/graphql/test"),
        text="first\n\nsecond " + ("x" * 600),
    ))
    snippet = response_body_snippet(rep)
    assert "\n" not in snippet
    assert snippet.startswith("first second")
    assert len(snippet) == 500


def test_no_account_error_distinguishes_in_use(tmp_path):
    """Two leased accounts are busy, not broken: the third caller must see all_in_use."""

    async def scenario():
        pool = AccountsPool(db_file=str(tmp_path / "accounts.db"), raise_when_no_account=True)
        await pool.add_account_cookies("one", "ct0=a; auth_token=b")
        await pool.add_account_cookies("two", "ct0=c; auth_token=d")
        # stale cause from an old failure must not leak into the next lease
        await pool.lock_until("one", "SearchTimeline", utc.ts() - 1, cause="unknown", error_status=500)
        assert await pool.get_for_queue("SearchTimeline") is not None
        assert await pool.get_for_queue("SearchTimeline") is not None
        with pytest.raises(NoAccountError) as exc:
            await pool.get_for_queue_or_wait("SearchTimeline")
        assert exc.value.cause == "all_in_use"

    run(scenario())


def test_xclid_diagnose_reports_parser_snippet():
    async def broken_home():
        return 'window.__chunks__=e=>(({abc:"main"})[e]||e)+"."+({broken:})[e]+"a.js"'

    result = run(check_xclid(fetch=broken_home))
    assert result["ok"] is False
    assert "snippet" in result
    assert "a.js" in result["snippet"]
