"""Accounts pool management for twscrape"""

import asyncio
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Literal, TypedDict

from fake_useragent import UserAgent
from httpx import HTTPStatusError

from .account import Account
from .db import execute, fetchall, fetchone
from .logger import logger
from .login import LoginConfig, login
from .utils import get_env_bool, parse_cookies, utc


NoAccountCause = Literal[
    "no_accounts_added",
    "all_rate_limited",
    "all_banned",
    "all_unknown_errors",
]


class NoAccountError(Exception):
    def __init__(
        self,
        queue: str,
        cause: NoAccountCause = "all_unknown_errors",
        *,
        next_unlock_at: datetime | None = None,
        message: str | None = None,
    ):
        self.queue = queue
        self.cause = cause
        self.next_unlock_at = next_unlock_at
        super().__init__(message or self._build_message())

    def _build_message(self) -> str:
        msg = f"No account available for queue {self.queue}: {self.cause}"
        if self.next_unlock_at:
            msg += f" until {self.next_unlock_at.isoformat()}"
        return msg


class AccountInfo(TypedDict):
    username: str
    logged_in: bool
    active: bool
    locks: dict[str, datetime]
    last_used: datetime | None
    total_req: int
    error_msg: str | None
    last_error_cause: str | None
    last_error_status: int | None
    last_error_body: str | None


def guess_delim(line: str):
    lp, rp = tuple([x.strip() for x in line.split("username")])
    return rp[0] if not lp else lp[-1]


class AccountsPool:
    # _order_by: str = "RANDOM()"
    _order_by: str = "username"

    def __init__(
        self,
        db_file="accounts.db",
        login_config: LoginConfig | None = None,
        raise_when_no_account=False,
    ):
        self._db_file = db_file
        self._login_config = login_config or LoginConfig()
        self._raise_when_no_account = raise_when_no_account

    async def load_from_file(self, filepath: str, line_format: str):
        line_delim = guess_delim(line_format)
        tokens = line_format.split(line_delim)

        required = set(["username", "password", "email", "email_password"])
        if not required.issubset(tokens):
            raise ValueError(f"Invalid line format: {line_format}")

        accounts = []
        with open(filepath, "r") as f:
            lines = f.read().split("\n")
            lines = [x.strip() for x in lines if x.strip()]

            for line in lines:
                data = [x.strip() for x in line.split(line_delim)]
                if len(data) < len(tokens):
                    raise ValueError(f"Invalid line: {line}")

                data = data[: len(tokens)]
                vals = {k: v for k, v in zip(tokens, data) if k != "_"}
                accounts.append(vals)

        for x in accounts:
            await self.add_account(**x)

    async def add_account(
        self,
        username: str,
        password: str,
        email: str,
        email_password: str,
        user_agent: str | None = None,
        proxy: str | None = None,
        cookies: str | None = None,
        mfa_code: str | None = None,
    ):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if rs:
            logger.warning(f"Account {username} already exists")
            return

        account = Account(
            username=username,
            password=password,
            email=email,
            email_password=email_password,
            user_agent=user_agent or UserAgent().safari,
            active=False,
            locks={},
            stats={},
            headers={},
            cookies=parse_cookies(cookies) if cookies else {},
            proxy=proxy,
            mfa_code=mfa_code,
        )

        if "ct0" in account.cookies:
            account.active = True

        await self.save(account)
        logger.info(f"Account {username} added successfully (active={account.active})")

    async def add_account_from_tokens(
        self,
        username: str,
        ct0: str,
        auth_token: str,
        user_agent: str | None = None,
    ):
        """Add account using pre-authenticated browser tokens (ct0 + auth_token)."""
        from .account import TOKEN

        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if rs:
            logger.warning(f"Account {username} already exists")
            return False

        cookies = {"ct0": ct0, "auth_token": auth_token}
        headers = {
            "authorization": TOKEN,
            "x-csrf-token": ct0,
            "x-twitter-auth-type": "OAuth2Session",
        }

        account = Account(
            username=username,
            password="token_auth",
            email="token@auth",
            email_password="token_auth",
            user_agent=user_agent or UserAgent().safari,
            active=True,
            locks={},
            stats={},
            headers=headers,
            cookies=cookies,
        )

        await self.save(account)
        logger.info(f"Account {username} added from tokens (active=True)")
        return True

    async def delete_accounts(self, usernames: str | list[str]):
        usernames = usernames if isinstance(usernames, list) else [usernames]
        usernames = list(set(usernames))
        if not usernames:
            logger.warning("No usernames provided")
            return

        qs = f"""DELETE FROM accounts WHERE username IN ({",".join([f'"{x}"' for x in usernames])})"""
        await execute(self._db_file, qs)

    async def delete_inactive(self):
        qs = "DELETE FROM accounts WHERE active = false"
        await execute(self._db_file, qs)

    async def get(self, username: str):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if not rs:
            raise ValueError(f"Account {username} not found")
        return Account.from_rs(rs)

    async def get_all(self):
        qs = "SELECT * FROM accounts"
        rs = await fetchall(self._db_file, qs)
        return [Account.from_rs(x) for x in rs]

    async def get_account(self, username: str):
        qs = "SELECT * FROM accounts WHERE username = :username"
        rs = await fetchone(self._db_file, qs, {"username": username})
        if not rs:
            return None
        return Account.from_rs(rs)

    async def save(self, account: Account):
        data = account.to_rs()
        cols = list(data.keys())

        qs = f"""
        INSERT INTO accounts ({",".join(cols)}) VALUES ({",".join([f":{x}" for x in cols])})
        ON CONFLICT(username) DO UPDATE SET {",".join([f"{x}=excluded.{x}" for x in cols])}
        """
        await execute(self._db_file, qs, data)

    async def login(self, account: Account):
        try:
            await login(account, cfg=self._login_config)
            logger.info(f"Logged in to {account.username} successfully")
            return True
        except HTTPStatusError as e:
            rep = e.response
            logger.error(f"Failed to login '{account.username}': {rep.status_code} - {rep.text}")
            return False
        except Exception as e:
            logger.error(f"Failed to login '{account.username}': {e}")
            return False
        finally:
            await self.save(account)

    async def login_all(self, usernames: list[str] | None = None):
        if usernames is None:
            qs = "SELECT * FROM accounts WHERE active = false AND error_msg IS NULL"
        else:
            us = ",".join([f'"{x}"' for x in usernames])
            qs = f"SELECT * FROM accounts WHERE username IN ({us})"

        rs = await fetchall(self._db_file, qs)
        accounts = [Account.from_rs(rs) for rs in rs]
        # await asyncio.gather(*[login(x) for x in self.accounts])

        counter = {"total": len(accounts), "success": 0, "failed": 0}
        for i, x in enumerate(accounts, start=1):
            logger.info(f"[{i}/{len(accounts)}] Logging in {x.username} - {x.email}")
            status = await self.login(x)
            counter["success" if status else "failed"] += 1
        return counter

    async def relogin(self, usernames: str | list[str]):
        usernames = usernames if isinstance(usernames, list) else [usernames]
        usernames = list(set(usernames))
        if not usernames:
            logger.warning("No usernames provided")
            return

        qs = f"""
        UPDATE accounts SET
            active = false,
            locks = json_object(),
            last_used = NULL,
            error_msg = NULL,
            headers = json_object(),
            cookies = json_object(),
            user_agent = "{UserAgent().safari}"
        WHERE username IN ({",".join([f'"{x}"' for x in usernames])})
        """

        await execute(self._db_file, qs)
        await self.login_all(usernames)

    async def relogin_failed(self):
        qs = "SELECT username FROM accounts WHERE active = false AND error_msg IS NOT NULL"
        rs = await fetchall(self._db_file, qs)
        await self.relogin([x["username"] for x in rs])

    async def reset_locks(self):
        qs = "UPDATE accounts SET locks = json_object()"
        await execute(self._db_file, qs)

    async def set_active(self, username: str, active: bool):
        qs = "UPDATE accounts SET active = :active WHERE username = :username"
        await execute(self._db_file, qs, {"username": username, "active": active})

    async def lock_until(
        self,
        username: str,
        queue: str,
        unlock_at: int,
        req_count=0,
        *,
        cause: str | None = None,
        error_status: int | None = None,
        error_body: str | None = None,
        error_msg: str | None = None,
    ):
        qs = f"""
        UPDATE accounts SET
            locks = json_set(locks, '$.{queue}', datetime({unlock_at}, 'unixepoch')),
            stats = json_set(stats, '$.{queue}', COALESCE(json_extract(stats, '$.{queue}'), 0) + {req_count}),
            last_used = datetime({utc.ts()}, 'unixepoch'),
            error_msg = COALESCE(:error_msg, error_msg),
            last_error_cause = :cause,
            last_error_status = :error_status,
            last_error_body = :error_body
        WHERE username = :username
        """
        await execute(
            self._db_file,
            qs,
            {
                "username": username,
                "cause": cause,
                "error_status": error_status,
                "error_body": error_body,
                "error_msg": error_msg,
            },
        )

    async def unlock(self, username: str, queue: str, req_count=0):
        qs = f"""
        UPDATE accounts SET
            locks = json_remove(locks, '$.{queue}'),
            stats = json_set(stats, '$.{queue}', COALESCE(json_extract(stats, '$.{queue}'), 0) + {req_count}),
            last_used = datetime({utc.ts()}, 'unixepoch')
        WHERE username = :username
        """
        await execute(self._db_file, qs, {"username": username})

    async def _get_and_lock(self, queue: str, condition: str):
        # if space in condition, it's a subquery, otherwise it's username
        condition = f"({condition})" if " " in condition else f"'{condition}'"

        if int(sqlite3.sqlite_version_info[1]) >= 35:
            qs = f"""
            UPDATE accounts SET
                locks = json_set(locks, '$.{queue}', datetime('now', '+15 minutes')),
                last_used = datetime({utc.ts()}, 'unixepoch')
            WHERE username = {condition}
            RETURNING *
            """
            rs = await fetchone(self._db_file, qs)
        else:
            tx = uuid.uuid4().hex
            qs = f"""
            UPDATE accounts SET
                locks = json_set(locks, '$.{queue}', datetime('now', '+15 minutes')),
                last_used = datetime({utc.ts()}, 'unixepoch'),
                _tx = '{tx}'
            WHERE username = {condition}
            """
            await execute(self._db_file, qs)

            qs = f"SELECT * FROM accounts WHERE _tx = '{tx}'"
            rs = await fetchone(self._db_file, qs)

        return Account.from_rs(rs) if rs else None

    async def get_for_queue(self, queue: str):
        q = f"""
        SELECT username FROM accounts
        WHERE active = true AND (
            locks IS NULL
            OR json_extract(locks, '$.{queue}') IS NULL
            OR json_extract(locks, '$.{queue}') < datetime('now')
        )
        ORDER BY {self._order_by}
        LIMIT 1
        """

        return await self._get_and_lock(queue, q)

    async def account_availability(self, queue: str):
        accounts = await self.get_all()
        now = utc.now()

        if not accounts:
            return {
                "cause": "no_accounts_added",
                "next_unlock_at": None,
                "accounts": accounts,
            }

        active = [x for x in accounts if x.active]
        if not active:
            return {
                "cause": "all_banned",
                "next_unlock_at": None,
                "accounts": accounts,
            }

        unavailable = []
        for account in active:
            locked_until = account.locks.get(queue)
            if locked_until and locked_until > now:
                unavailable.append((account, locked_until))

        if len(unavailable) == len(active):
            next_unlock_at = min(x[1] for x in unavailable)
            causes = {x[0].last_error_cause for x in unavailable}
            if "unknown" in causes:
                cause = "all_unknown_errors"
            else:
                cause = "all_rate_limited"

            return {
                "cause": cause,
                "next_unlock_at": next_unlock_at,
                "accounts": accounts,
            }

        return {
            "cause": "all_unknown_errors",
            "next_unlock_at": None,
            "accounts": accounts,
        }

    async def get_for_queue_or_wait(self, queue: str) -> Account | None:
        msg_shown = False
        while True:
            account = await self.get_for_queue(queue)
            if not account:
                availability = await self.account_availability(queue)
                if self._raise_when_no_account or get_env_bool("TWS_RAISE_WHEN_NO_ACCOUNT"):
                    raise NoAccountError(
                        queue,
                        availability["cause"],
                        next_unlock_at=availability["next_unlock_at"],
                    )

                if not msg_shown:
                    nat = availability["next_unlock_at"]
                    if not nat:
                        logger.warning("No active accounts. Stopping...")
                        return None

                    at_local = datetime.now() + (nat - utc.now())
                    msg = f'No account available for queue "{queue}". Next available at {at_local:%H:%M:%S}'
                    logger.info(msg)
                    msg_shown = True

                await asyncio.sleep(5)
                continue
            else:
                if msg_shown:
                    logger.info(f"Continuing with account {account.username} on queue {queue}")

            return account

    async def next_available_at(self, queue: str):
        qs = f"""
        SELECT json_extract(locks, '$."{queue}"') as lock_until
        FROM accounts
        WHERE active = true AND json_extract(locks, '$."{queue}"') IS NOT NULL
        ORDER BY lock_until ASC
        LIMIT 1
        """
        rs = await fetchone(self._db_file, qs)
        if rs:
            now, trg = utc.now(), utc.from_iso(rs[0])
            if trg < now:
                return "now"

            at_local = datetime.now() + (trg - now)
            return at_local.strftime("%H:%M:%S")

        return None

    async def mark_inactive(
        self,
        username: str,
        error_msg: str | None,
        *,
        cause: str | None = None,
        error_status: int | None = None,
        error_body: str | None = None,
    ):
        qs = """
        UPDATE accounts SET
            active = false,
            error_msg = :error_msg,
            last_error_cause = :cause,
            last_error_status = :error_status,
            last_error_body = :error_body,
            last_used = datetime(:last_used, 'unixepoch')
        WHERE username = :username
        """
        await execute(
            self._db_file,
            qs,
            {
                "username": username,
                "error_msg": error_msg,
                "cause": cause,
                "error_status": error_status,
                "error_body": error_body,
                "last_used": utc.ts(),
            },
        )

    async def stats(self):
        def locks_count(queue: str):
            return f"""
            SELECT COUNT(*) FROM accounts
            WHERE json_extract(locks, '$.{queue}') IS NOT NULL
                AND json_extract(locks, '$.{queue}') > datetime('now')
            """

        qs = "SELECT DISTINCT(f.key) as k from accounts, json_each(locks) f"
        rs = await fetchall(self._db_file, qs)
        gql_ops = [x["k"] for x in rs]

        config = [
            ("total", "SELECT COUNT(*) FROM accounts"),
            ("active", "SELECT COUNT(*) FROM accounts WHERE active = true"),
            ("inactive", "SELECT COUNT(*) FROM accounts WHERE active = false"),
            *[(f"locked_{x}", locks_count(x)) for x in gql_ops],
        ]

        qs = f"SELECT {','.join([f'({q}) as {k}' for k, q in config])}"
        rs = await fetchone(self._db_file, qs)
        return dict(rs) if rs else {}

    async def accounts_info(self):
        accounts = await self.get_all()

        items: list[AccountInfo] = []
        for x in accounts:
            item: AccountInfo = {
                "username": x.username,
                "logged_in": (x.headers or {}).get("authorization", "") != "",
                "active": x.active,
                "locks": x.locks,
                "last_used": x.last_used,
                "total_req": sum(x.stats.values()),
                "error_msg": str(x.error_msg)[0:60] if x.error_msg else None,
                "last_error_cause": x.last_error_cause,
                "last_error_status": x.last_error_status,
                "last_error_body": x.last_error_body,
            }
            items.append(item)

        old_time = datetime(1970, 1, 1).replace(tzinfo=timezone.utc)
        items = sorted(items, key=lambda x: x["username"].lower())
        items = sorted(
            items,
            key=lambda x: x["last_used"] or old_time if x["total_req"] > 0 else old_time,
            reverse=True,
        )
        items = sorted(items, key=lambda x: x["active"], reverse=True)
        # items = sorted(items, key=lambda x: x["total_req"], reverse=True)
        return items
