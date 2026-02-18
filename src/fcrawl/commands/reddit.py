"""Reddit commands for fcrawl.

Read-only access to Reddit via public .json endpoints.
No authentication or API keys required.
"""

import json
import re
import shlex
from datetime import datetime, timezone
from typing import Any, Optional

import click
from rich.console import Console
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.output import save_to_file, resolve_pretty
from ..utils.reddit_client import RedditClient

console = Console()

POST_ID_RE = re.compile(r"^[a-z0-9]{5,10}$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def format_number(n: int | None) -> str:
    """Format a number for display (1.2K, 3.4M, etc.)."""
    if n is None:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def format_timestamp(utc: float | int | None) -> str:
    """Convert Unix timestamp to human-readable relative time."""
    if utc is None:
        return "unknown"

    now = datetime.now(timezone.utc)
    dt = datetime.fromtimestamp(utc, tz=timezone.utc)
    delta = now - dt

    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days < 30:
        return f"{days}d ago"
    months = days // 30
    if months < 12:
        return f"{months}mo ago"
    years = days // 365
    return f"{years}y ago"


def format_date(utc: float | int | None) -> str:
    """Convert Unix timestamp to date string."""
    if utc is None:
        return "unknown"
    dt = datetime.fromtimestamp(utc, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _truncate(text: str | None, length: int = 80) -> str:
    """Truncate text to length."""
    if not text:
        return ""
    value = text.replace("\n", " ").strip()
    if len(value) <= length:
        return value
    return value[: length - 3] + "..."


def _absolute_reddit_url(path_or_url: str | None) -> str:
    """Convert Reddit path to absolute URL if needed."""
    if not path_or_url:
        return ""
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    if not path_or_url.startswith("/"):
        path_or_url = "/" + path_or_url
    return f"https://www.reddit.com{path_or_url}"


def _normalize_subreddit(name: str) -> str:
    """Normalize subreddit input to bare name."""
    value = name.strip().strip("/")
    if value.lower().startswith("r/"):
        value = value[2:]
    if not value:
        raise click.BadParameter("subreddit cannot be empty")
    return value


def _normalize_username(username: str) -> str:
    """Normalize username input to bare username."""
    value = username.strip().strip("/")
    if value.lower().startswith("u/"):
        value = value[2:]
    if not value:
        raise click.BadParameter("username cannot be empty")
    return value


def _is_share_target(target: str) -> bool:
    """Return True when target matches Reddit /s/ share URL pattern."""
    return bool(
        re.fullmatch(
            r"(?:(?:https?://)?(?:www\.|old\.)?reddit\.com)?/r/[^/]+/s/[^/]+",
            target,
            re.IGNORECASE,
        )
    )


def _resolve_share_target(target: str, client: RedditClient) -> str:
    """Resolve Reddit /s/ share URL via HTTP redirect."""
    if target.startswith("/"):
        target = f"https://www.reddit.com{target}"
    elif not target.startswith("http://") and not target.startswith("https://"):
        target = f"https://{target}"

    resolved = client.resolve_share_url(target)
    return resolved.split("?")[0].split("#")[0].strip().rstrip("/")


def _parse_post_target(url_or_id: str, client: Optional[RedditClient] = None) -> str:
    """Extract post path from URL/short URL/path/post ID.

    Accepted inputs:
    - https://www.reddit.com/r/<sub>/comments/<id>/<slug>/
    - https://old.reddit.com/r/<sub>/comments/<id>/<slug>/
    - https://www.reddit.com/r/<sub>/s/<token>
    - https://reddit.com/comments/<id>
    - https://redd.it/<id>
    - /r/<sub>/comments/<id>/<slug>/
    - <id>

    Returns:
        comments/<id>
    """
    target = url_or_id.strip().split("?")[0].split("#")[0].strip().rstrip("/")

    if client and _is_share_target(target):
        try:
            target = _resolve_share_target(target, client)
        except Exception as exc:
            raise ValueError(
                f"Cannot resolve Reddit share URL: {url_or_id} ({exc})"
            ) from exc

    if POST_ID_RE.fullmatch(target):
        return f"comments/{target.lower()}"

    if target.startswith("/"):
        match = re.search(r"/comments/([a-z0-9]+)", target, re.IGNORECASE)
        if match:
            return f"comments/{match.group(1).lower()}"

    full_url_patterns = [
        r"(?:https?://)?(?:www\.|old\.)?reddit\.com/(?:r/[^/]+/)?comments/([a-z0-9]+)",
        r"(?:https?://)?redd\.it/([a-z0-9]+)",
    ]
    for pattern in full_url_patterns:
        match = re.search(pattern, target, re.IGNORECASE)
        if match:
            return f"comments/{match.group(1).lower()}"

    raise ValueError(f"Cannot parse Reddit URL/ID: {url_or_id}")


def _print_json_or_save(
    data: Any, output: Optional[str], json_output: bool, pretty: bool
):
    """Emit JSON output to file and/or terminal."""
    text = json.dumps(data, indent=2 if pretty else None)
    if output:
        save_to_file(text, output, "json")
    if json_output and not output:
        if pretty:
            console.print_json(text)
        else:
            print(text)


def _fetch_with_cache(
    client: RedditClient,
    cache_bucket: str,
    path: str,
    params: dict[str, Any],
    no_cache: bool,
    cache_only: bool,
    progress_label: str,
) -> tuple[Any, bool]:
    """Fetch Reddit JSON with on-disk cache support."""
    key = cache_key(path, params)

    if not no_cache:
        cached = read_cache(cache_bucket, key)
        if cached is not None:
            console.print("[dim]Using cached result[/dim]")
            return cached, True

    if cache_only:
        console.print(f"[red]Not in cache: {path}[/red]")
        raise click.Abort()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task(progress_label, total=None)
        try:
            result = client.get(path, params)
        except Exception as exc:
            console.print(f"[red]Error: {exc}[/red]")
            raise click.Abort()

    write_cache(cache_bucket, key, result)
    return result, False


def _print_next_after_hint(command: str, next_after: str, pretty: bool):
    """Print pagination continuation hint."""
    hint = f"Next page: {command} --after {next_after}"
    if pretty:
        console.print(f"[dim]{hint}[/dim]")
    else:
        print(hint)


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def display_post_table(posts: list[dict]):
    """Display a list of posts as a Rich table."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("#", justify="right", style="dim", max_width=4)
    table.add_column("Sub", style="cyan", max_width=18)
    table.add_column("Author", style="blue", max_width=18)
    table.add_column("Score", justify="right", style="green", max_width=7)
    table.add_column("Cmt", justify="right", style="yellow", max_width=6)
    table.add_column("Age", style="dim", max_width=8)
    table.add_column("Title")

    for idx, post in enumerate(posts, 1):
        data = post.get("data", post)
        sub = data.get("subreddit", "")
        author = data.get("author", "[deleted]")
        score = format_number(data.get("score", 0))
        comments = format_number(data.get("num_comments", 0))
        age = format_timestamp(data.get("created_utc"))
        title = _truncate(data.get("title", ""), 74)
        flair = data.get("link_flair_text")
        if flair:
            title = f"[{flair}] {title}"

        permalink = _absolute_reddit_url(data.get("permalink", ""))
        title_cell = escape(title)
        if permalink:
            title_cell = f"[link={permalink}]{title_cell}[/link]"

        table.add_row(
            str(idx),
            f"r/{sub}",
            f"u/{author}",
            score,
            comments,
            age,
            title_cell,
        )

    console.print(table)


def display_post_lines(posts: list[dict]):
    """Display a list of posts as plain text lines."""
    for idx, post in enumerate(posts, 1):
        data = post.get("data", post)
        title = data.get("title", "")
        sub = data.get("subreddit", "")
        author = data.get("author", "[deleted]")
        score = format_number(data.get("score", 0))
        comments = format_number(data.get("num_comments", 0))
        age = format_timestamp(data.get("created_utc"))
        permalink = _absolute_reddit_url(data.get("permalink", ""))
        snippet = _truncate(data.get("selftext", ""), 140)

        print(f"{idx}. {title}")
        print(f"   r/{sub} | u/{author} | {score} pts | {comments} comments | {age}")
        if permalink:
            print(f"   {permalink}")
        if snippet:
            print(f"   {snippet}")


def display_post(post_data: dict, show_body: bool = True, pretty: bool = True):
    """Display a single post."""
    data = post_data.get("data", post_data)

    title = data.get("title", "")
    author = data.get("author", "[deleted]")
    subreddit = data.get("subreddit", "")
    score = format_number(data.get("score", 0))
    comments_count = format_number(data.get("num_comments", 0))
    awards = format_number(data.get("total_awards_received", 0))
    age = format_timestamp(data.get("created_utc"))
    flair = data.get("link_flair_text")
    selftext = data.get("selftext", "")
    permalink_url = _absolute_reddit_url(data.get("permalink", ""))
    external_url = _absolute_reddit_url(data.get("url", ""))

    if pretty:
        console.print("=" * 70)
        console.print(f"[bold]{title}[/bold]")
        if flair:
            console.print(f"[magenta][{flair}][/magenta]")
        console.print(
            f"[cyan]r/{subreddit}[/cyan] | "
            f"[dim]u/{author}[/dim] | "
            f"[green]{score} pts[/green] | "
            f"[yellow]{comments_count} comments[/yellow] | "
            f"[magenta]{awards} awards[/magenta] | "
            f"[dim]{age}[/dim]"
        )
        if external_url and external_url != permalink_url:
            console.print(f"[blue]{external_url}[/blue]")
        if permalink_url:
            console.print(f"[dim]{permalink_url}[/dim]")
        if show_body and selftext:
            console.print()
            console.print(selftext)
        console.print()
        return

    print(title)
    print(
        f"r/{subreddit} | u/{author} | {score} pts | "
        f"{comments_count} comments | {awards} awards | {age}"
    )
    if external_url and external_url != permalink_url:
        print(external_url)
    if permalink_url:
        print(permalink_url)
    if show_body and selftext:
        print()
        print(selftext)
    print()


def display_comment_tree(
    children: list,
    depth: int = 0,
    max_depth: int = 3,
    pretty: bool = True,
):
    """Recursively display a comment tree with indentation."""
    for child in children:
        if child.get("kind") != "t1":
            continue

        data = child.get("data", {})
        author = data.get("author", "[deleted]")
        score = format_number(data.get("score", 0))
        age = format_timestamp(data.get("created_utc"))
        body = data.get("body", "")
        indent = "  " * depth

        if pretty:
            bar = "[dim]|[/dim] " * depth
            console.print(
                f"{bar}[bold cyan]u/{author}[/bold cyan] "
                f"[green]({score} pts)[/green] "
                f"[dim]{age}[/dim]"
            )
            if body and body != "[deleted]":
                for line in body.split("\n"):
                    if line.strip():
                        console.print(f"{bar}  {line}")
            console.print(f"{bar}")
        else:
            print(f"{indent}u/{author} ({score} pts) {age}")
            if body and body != "[deleted]":
                for line in body.split("\n"):
                    if line.strip():
                        print(f"{indent}  {line}")
            print()

        if depth < max_depth - 1:
            replies = data.get("replies")
            if replies and isinstance(replies, dict):
                reply_children = replies.get("data", {}).get("children", [])
                if reply_children:
                    display_comment_tree(
                        reply_children,
                        depth=depth + 1,
                        max_depth=max_depth,
                        pretty=pretty,
                    )


def display_subreddit_about(data: dict, pretty: bool = True):
    """Display subreddit info."""
    d = data.get("data", data)
    name = d.get("display_name", "")
    title = d.get("title", "")
    desc = d.get("public_description", "") or d.get("description", "")
    subscribers = format_number(d.get("subscribers", 0))
    active = format_number(d.get("accounts_active", 0))
    created = format_date(d.get("created_utc"))
    nsfw = d.get("over18", False)

    if pretty:
        console.print("=" * 60)
        console.print(f"[bold cyan]r/{name}[/bold cyan]")
        if title and title != name:
            console.print(f"[bold]{title}[/bold]")
        if nsfw:
            console.print("[red]NSFW[/red]")
        console.print()
        if desc:
            console.print(desc.strip())
            console.print()
        console.print(f"[bold]Subscribers:[/bold] {subscribers}")
        console.print(f"[bold]Active:[/bold] {active}")
        console.print(f"[bold]Created:[/bold] {created}")
        console.print("=" * 60)
        return

    print(f"r/{name}")
    if title and title != name:
        print(title)
    if nsfw:
        print("NSFW")
    if desc:
        print(desc.strip())
    print(f"Subscribers: {subscribers}")
    print(f"Active: {active}")
    print(f"Created: {created}")


def display_user_about(data: dict, pretty: bool = True):
    """Display user profile info."""
    d = data.get("data", data)
    name = d.get("name", "")
    comment_karma = format_number(d.get("comment_karma", 0))
    link_karma = format_number(d.get("link_karma", 0))
    total_karma = format_number(d.get("total_karma", 0))
    created = format_date(d.get("created_utc"))
    desc = ""
    subreddit_blob = d.get("subreddit")
    if isinstance(subreddit_blob, dict):
        desc = subreddit_blob.get("public_description", "")
    is_gold = d.get("is_gold", False)
    verified = d.get("verified", False)

    if pretty:
        console.print("=" * 60)
        console.print(f"[bold cyan]u/{name}[/bold cyan]")
        badges = []
        if is_gold:
            badges.append("[yellow]Gold[/yellow]")
        if verified:
            badges.append("[blue]Verified[/blue]")
        if badges:
            console.print(" ".join(badges))
        console.print()
        if desc:
            console.print(desc.strip())
            console.print()
        console.print(f"[bold]Total Karma:[/bold] {total_karma}")
        console.print(f"[bold]Post Karma:[/bold] {link_karma}")
        console.print(f"[bold]Comment Karma:[/bold] {comment_karma}")
        console.print(f"[bold]Account Created:[/bold] {created}")
        console.print("=" * 60)
        return

    print(f"u/{name}")
    badges = []
    if is_gold:
        badges.append("Gold")
    if verified:
        badges.append("Verified")
    if badges:
        print(" ".join(badges))
    if desc:
        print(desc.strip())
    print(f"Total Karma: {total_karma}")
    print(f"Post Karma: {link_karma}")
    print(f"Comment Karma: {comment_karma}")
    print(f"Account Created: {created}")


def display_user_activity(items: list, pretty: bool = True):
    """Display a user's mixed activity (posts + comments)."""
    for item in items:
        kind = item.get("kind", "")
        data = item.get("data", {})

        if kind == "t3":
            sub = data.get("subreddit", "")
            title = _truncate(data.get("title", ""), 80)
            score = format_number(data.get("score", 0))
            age = format_timestamp(data.get("created_utc"))
            permalink = _absolute_reddit_url(data.get("permalink", ""))

            if pretty:
                console.print(
                    f"[bold blue][post][/bold blue] "
                    f"[cyan]r/{sub}[/cyan] "
                    f"[green]{score} pts[/green] "
                    f"[dim]{age}[/dim]"
                )
                console.print(f"  {title}")
                if permalink:
                    console.print(f"  [dim]{permalink}[/dim]")
            else:
                print(f"[post] r/{sub} {score} pts {age}")
                print(f"  {title}")
                if permalink:
                    print(f"  {permalink}")
            print()
            continue

        if kind == "t1":
            sub = data.get("subreddit", "")
            body = _truncate(data.get("body", ""), 120)
            score = format_number(data.get("score", 0))
            age = format_timestamp(data.get("created_utc"))
            link_title = _truncate(data.get("link_title", ""), 60)
            permalink = _absolute_reddit_url(data.get("permalink", ""))

            if pretty:
                console.print(
                    f"[bold yellow][comment][/bold yellow] "
                    f"[cyan]r/{sub}[/cyan] "
                    f"[green]{score} pts[/green] "
                    f"[dim]{age}[/dim]"
                )
                if link_title:
                    console.print(f"  [dim]on: {link_title}[/dim]")
                console.print(f"  {body}")
                if permalink:
                    console.print(f"  [dim]{permalink}[/dim]")
            else:
                print(f"[comment] r/{sub} {score} pts {age}")
                if link_title:
                    print(f"  on: {link_title}")
                print(f"  {body}")
                if permalink:
                    print(f"  {permalink}")
            print()


# ---------------------------------------------------------------------------
# JSON serializers
# ---------------------------------------------------------------------------


def post_to_dict(data: dict) -> dict:
    """Convert Reddit post data to a JSON-friendly dict."""
    d = data.get("data", data)
    return {
        "id": d.get("id"),
        "title": d.get("title"),
        "author": d.get("author"),
        "subreddit": d.get("subreddit"),
        "score": d.get("score"),
        "upvote_ratio": d.get("upvote_ratio"),
        "num_comments": d.get("num_comments"),
        "total_awards_received": d.get("total_awards_received"),
        "url": _absolute_reddit_url(d.get("url")),
        "permalink": _absolute_reddit_url(d.get("permalink")),
        "selftext": d.get("selftext"),
        "created_utc": d.get("created_utc"),
        "flair": d.get("link_flair_text"),
    }


def comment_to_dict(
    child: dict,
    max_depth: int = 3,
    depth: int = 0,
) -> Optional[dict]:
    """Convert Reddit comment to dict, recursively including replies."""
    if child.get("kind") != "t1":
        return None

    data = child.get("data", {})
    result = {
        "id": data.get("id"),
        "author": data.get("author"),
        "subreddit": data.get("subreddit"),
        "score": data.get("score"),
        "body": data.get("body"),
        "parent_id": data.get("parent_id"),
        "permalink": _absolute_reddit_url(data.get("permalink")),
        "created_utc": data.get("created_utc"),
        "depth": depth,
    }

    if depth < max_depth - 1:
        replies = data.get("replies")
        if replies and isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            result["replies"] = [
                c
                for c in (
                    comment_to_dict(reply, max_depth=max_depth, depth=depth + 1)
                    for reply in reply_children
                )
                if c is not None
            ]

    return result


def user_to_dict(data: dict) -> dict:
    """Convert user about data to dict."""
    d = data.get("data", data)
    bio = ""
    subreddit_blob = d.get("subreddit")
    if isinstance(subreddit_blob, dict):
        bio = subreddit_blob.get("public_description") or subreddit_blob.get(
            "description"
        )

    return {
        "name": d.get("name"),
        "total_karma": d.get("total_karma"),
        "link_karma": d.get("link_karma"),
        "comment_karma": d.get("comment_karma"),
        "created_utc": d.get("created_utc"),
        "is_gold": d.get("is_gold"),
        "verified": d.get("verified"),
        "bio": bio,
    }


def subreddit_to_dict(data: dict) -> dict:
    """Convert subreddit about data to dict."""
    d = data.get("data", data)
    return {
        "name": d.get("display_name"),
        "title": d.get("title"),
        "description": d.get("public_description") or d.get("description"),
        "subscribers": d.get("subscribers"),
        "active_users": d.get("accounts_active"),
        "created_utc": d.get("created_utc"),
        "nsfw": d.get("over18"),
    }


def activity_item_to_dict(item: dict) -> Optional[dict]:
    """Convert user activity listing item to dict."""
    kind = item.get("kind", "")
    if kind == "t3":
        return {"type": "post", **post_to_dict(item)}

    if kind == "t1":
        data = item.get("data", {})
        return {
            "type": "comment",
            "id": data.get("id"),
            "author": data.get("author"),
            "subreddit": data.get("subreddit"),
            "score": data.get("score"),
            "body": data.get("body"),
            "link_title": data.get("link_title"),
            "link_permalink": _absolute_reddit_url(data.get("link_permalink")),
            "permalink": _absolute_reddit_url(data.get("permalink")),
            "created_utc": data.get("created_utc"),
        }

    return None


# ---------------------------------------------------------------------------
# Click command group
# ---------------------------------------------------------------------------


@click.group()
def reddit():
    """Reddit commands - search, read posts, browse subreddits and users.

    \b
    Examples:
        fcrawl reddit search "Claude Code" -l 10
        fcrawl reddit post https://reddit.com/r/sub/comments/id/slug/
        fcrawl reddit post 1abc234
        fcrawl reddit subreddit python -l 20
        fcrawl reddit user spez
    """
    pass


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


@reddit.command(name="search")
@click.argument("query")
@click.option("--subreddit", "-s", default=None, help="Restrict search to a subreddit")
@click.option("--user", "-u", default=None, help="Restrict search to a specific author")
@click.option(
    "--sort",
    type=click.Choice(["relevance", "hot", "top", "new", "comments"]),
    default="relevance",
    help="Sort order (default: relevance)",
)
@click.option(
    "--time",
    "-t",
    "time_filter",
    type=click.Choice(["hour", "day", "week", "month", "year", "all"]),
    default="all",
    help="Time filter (default: all)",
)
@click.option(
    "--limit", "-l", type=int, default=20, help="Max results (default: 20, max: 100)"
)
@click.option("--after", default=None, help="Pagination cursor from previous result")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option(
    "--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch"
)
@click.option("--cache-only", "cache_only", is_flag=True, help="Only read from cache")
def reddit_search(
    query: str,
    subreddit: Optional[str],
    user: Optional[str],
    sort: str,
    time_filter: str,
    limit: int,
    after: Optional[str],
    output: Optional[str],
    json_output: bool,
    pretty: Optional[bool],
    no_cache: bool,
    cache_only: bool,
):
    """Search Reddit posts.

    \b
    Examples:
        fcrawl reddit search "python async"
        fcrawl reddit search "hooks" -s ClaudeCode --sort top
        fcrawl reddit search "mcp server" --user spez --time week -l 10
    """
    pretty = resolve_pretty(pretty)

    if limit < 1:
        raise click.BadParameter("limit must be >= 1", param_hint="--limit")

    limit = min(limit, 100)
    subreddit_name = _normalize_subreddit(subreddit) if subreddit else None
    author_name = _normalize_username(user) if user else None

    search_query = query
    if author_name:
        search_query = f"author:{author_name} {query}"

    if subreddit_name:
        path = f"r/{subreddit_name}/search"
        params: dict[str, Any] = {
            "q": search_query,
            "restrict_sr": "on",
            "sort": sort,
            "t": time_filter,
            "limit": limit,
        }
    else:
        path = "search"
        params = {
            "q": search_query,
            "sort": sort,
            "t": time_filter,
            "limit": limit,
        }

    if after:
        params["after"] = after

    client = RedditClient()
    desc = f"Searching Reddit for '{query}'"
    if subreddit_name:
        desc += f" in r/{subreddit_name}"

    result, from_cache = _fetch_with_cache(
        client=client,
        cache_bucket="reddit-search",
        path=path,
        params=params,
        no_cache=no_cache,
        cache_only=cache_only,
        progress_label=f"{desc}...",
    )

    listing = result.get("data", {})
    posts = listing.get("children", [])
    next_after = listing.get("after")

    if not posts:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(posts)} results[/green]")

    if json_output or output:
        output_data = {
            "query": query,
            "filters": {
                "subreddit": subreddit_name,
                "user": author_name,
                "sort": sort,
                "time": time_filter,
                "after": after,
            },
            "results": [post_to_dict(p) for p in posts],
            "meta": {
                "count": len(posts),
                "next_after": next_after,
                "from_cache": from_cache,
            },
        }
        _print_json_or_save(output_data, output, json_output, pretty)
        return

    if pretty:
        display_post_table(posts)
    else:
        display_post_lines(posts)

    if next_after:
        cmd = f"fcrawl reddit search {shlex.quote(query)}"
        if subreddit_name:
            cmd += f" -s {shlex.quote(subreddit_name)}"
        if author_name:
            cmd += f" -u {shlex.quote(author_name)}"
        cmd += f" --sort {sort} --time {time_filter} -l {limit}"
        _print_next_after_hint(cmd, next_after, pretty)


# ---------------------------------------------------------------------------
# post
# ---------------------------------------------------------------------------


@reddit.command(name="post")
@click.argument("url_or_id")
@click.option(
    "--sort",
    type=click.Choice(["best", "top", "new", "controversial", "old", "qa"]),
    default="best",
    help="Comment sort order (default: best)",
)
@click.option(
    "--limit",
    "-l",
    type=int,
    default=20,
    help="Max top-level comments (default: 20, 0=all)",
)
@click.option(
    "--depth", "-d", type=int, default=3, help="Comment nesting depth (default: 3)"
)
@click.option(
    "--no-comments", is_flag=True, help="Fetch only the post, skip comment output"
)
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option(
    "--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch"
)
@click.option("--cache-only", "cache_only", is_flag=True, help="Only read from cache")
def reddit_post(
    url_or_id: str,
    sort: str,
    limit: int,
    depth: int,
    no_comments: bool,
    output: Optional[str],
    json_output: bool,
    pretty: Optional[bool],
    no_cache: bool,
    cache_only: bool,
):
    """Fetch a Reddit post with comments.

    \b
    Examples:
        fcrawl reddit post https://reddit.com/r/python/comments/abc123/my_post/
        fcrawl reddit post 1abc234 --limit 5 --depth 2
        fcrawl reddit post URL --sort top --no-comments
    """
    pretty = resolve_pretty(pretty)

    if limit < 0:
        raise click.BadParameter("limit must be >= 0", param_hint="--limit")
    if depth < 1:
        raise click.BadParameter("depth must be >= 1", param_hint="--depth")

    client = RedditClient()

    try:
        path = _parse_post_target(url_or_id, client=client)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise click.Abort()

    params: dict[str, Any] = {"sort": sort, "depth": depth}
    if no_comments:
        params["limit"] = 0
    elif limit > 0:
        params["limit"] = limit

    result, from_cache = _fetch_with_cache(
        client=client,
        cache_bucket="reddit-post",
        path=path,
        params=params,
        no_cache=no_cache,
        cache_only=cache_only,
        progress_label="Fetching post...",
    )

    if not isinstance(result, list) or len(result) < 2:
        console.print("[red]Unexpected response format[/red]")
        raise click.Abort()

    post_children = result[0].get("data", {}).get("children", [])
    comment_children = result[1].get("data", {}).get("children", [])

    if not post_children:
        console.print("[yellow]Post not found[/yellow]")
        return

    post = post_children[0]
    comments_payload = []
    if not no_comments:
        comments_payload = [
            comment
            for comment in (
                comment_to_dict(child, max_depth=depth) for child in comment_children
            )
            if comment is not None
        ]

    if json_output or output:
        output_data = {
            "post": post_to_dict(post),
            "comments": comments_payload,
            "meta": {
                "sort": sort,
                "depth": depth,
                "limit": limit,
                "no_comments": no_comments,
                "from_cache": from_cache,
                "returned_top_level": len(comment_children),
            },
        }
        _print_json_or_save(output_data, output, json_output, pretty)
        return

    display_post(post, show_body=True, pretty=pretty)
    if no_comments:
        return

    if comment_children:
        if pretty:
            console.print("[bold]Comments[/bold]")
            console.print("-" * 40)
        else:
            print("Comments")
            print("-" * 40)
        display_comment_tree(comment_children, depth=0, max_depth=depth, pretty=pretty)


# ---------------------------------------------------------------------------
# subreddit
# ---------------------------------------------------------------------------


@reddit.command(name="subreddit")
@click.argument("name")
@click.option(
    "--sort",
    type=click.Choice(["hot", "new", "top", "rising"]),
    default="hot",
    help="Sort order (default: hot)",
)
@click.option(
    "--time",
    "-t",
    "time_filter",
    type=click.Choice(["hour", "day", "week", "month", "year", "all"]),
    default="all",
    help="Time filter for --sort top (default: all)",
)
@click.option(
    "--limit", "-l", type=int, default=20, help="Max posts (default: 20, max: 100)"
)
@click.option("--after", default=None, help="Pagination cursor from previous result")
@click.option("--about", is_flag=True, help="Show subreddit info instead of feed")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option(
    "--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch"
)
@click.option("--cache-only", "cache_only", is_flag=True, help="Only read from cache")
def reddit_subreddit(
    name: str,
    sort: str,
    time_filter: str,
    limit: int,
    after: Optional[str],
    about: bool,
    output: Optional[str],
    json_output: bool,
    pretty: Optional[bool],
    no_cache: bool,
    cache_only: bool,
):
    """Browse a subreddit's feed or info.

    \b
    Examples:
        fcrawl reddit subreddit python
        fcrawl reddit subreddit ClaudeCode --sort top --time week
        fcrawl reddit subreddit python --after t3_1abc234
    """
    pretty = resolve_pretty(pretty)

    if limit < 1:
        raise click.BadParameter("limit must be >= 1", param_hint="--limit")

    limit = min(limit, 100)
    subreddit_name = _normalize_subreddit(name)
    client = RedditClient()

    if about:
        path = f"r/{subreddit_name}/about"
        params: dict[str, Any] = {}
        label = f"Fetching r/{subreddit_name} info..."
        cache_bucket = "reddit-subreddit-about"
    else:
        path = f"r/{subreddit_name}/{sort}"
        params = {"t": time_filter, "limit": limit}
        if after:
            params["after"] = after
        label = f"Fetching r/{subreddit_name}/{sort}..."
        cache_bucket = "reddit-subreddit-feed"

    result, from_cache = _fetch_with_cache(
        client=client,
        cache_bucket=cache_bucket,
        path=path,
        params=params,
        no_cache=no_cache,
        cache_only=cache_only,
        progress_label=label,
    )

    if about:
        payload = subreddit_to_dict(result)
        if json_output or output:
            output_data = {
                "subreddit": payload,
                "meta": {"from_cache": from_cache},
            }
            _print_json_or_save(output_data, output, json_output, pretty)
            return

        display_subreddit_about(result, pretty=pretty)
        return

    listing = result.get("data", {})
    posts = listing.get("children", [])
    next_after = listing.get("after")
    if not posts:
        console.print(f"[yellow]No posts found in r/{subreddit_name}[/yellow]")
        return

    console.print(f"[green]Found {len(posts)} posts in r/{subreddit_name}[/green]")

    if json_output or output:
        output_data = {
            "subreddit": subreddit_name,
            "sort": sort,
            "time": time_filter,
            "results": [post_to_dict(post) for post in posts],
            "meta": {
                "count": len(posts),
                "next_after": next_after,
                "from_cache": from_cache,
            },
        }
        _print_json_or_save(output_data, output, json_output, pretty)
        return

    if pretty:
        display_post_table(posts)
    else:
        display_post_lines(posts)

    if next_after:
        cmd = (
            f"fcrawl reddit subreddit {shlex.quote(subreddit_name)} "
            f"--sort {sort} --time {time_filter} -l {limit}"
        )
        _print_next_after_hint(cmd, next_after, pretty)


# ---------------------------------------------------------------------------
# user
# ---------------------------------------------------------------------------


@reddit.command(name="user")
@click.argument("username")
@click.option("--about", is_flag=True, help="Show profile info only")
@click.option("--posts-only", is_flag=True, help="Show only submitted posts")
@click.option("--comments-only", is_flag=True, help="Show only comments")
@click.option(
    "--type",
    "activity_type",
    type=click.Choice(["overview", "submitted", "comments"]),
    default="overview",
    help="Activity type (default: overview)",
)
@click.option(
    "--sort",
    type=click.Choice(["hot", "new", "top", "controversial"]),
    default="new",
    help="Sort order (default: new)",
)
@click.option(
    "--time",
    "-t",
    "time_filter",
    type=click.Choice(["hour", "day", "week", "month", "year", "all"]),
    default="all",
    help="Time filter for --sort top (default: all)",
)
@click.option(
    "--limit", "-l", type=int, default=20, help="Max items (default: 20, max: 100)"
)
@click.option("--after", default=None, help="Pagination cursor from previous result")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option(
    "--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch"
)
@click.option("--cache-only", "cache_only", is_flag=True, help="Only read from cache")
def reddit_user(
    username: str,
    about: bool,
    posts_only: bool,
    comments_only: bool,
    activity_type: str,
    sort: str,
    time_filter: str,
    limit: int,
    after: Optional[str],
    output: Optional[str],
    json_output: bool,
    pretty: Optional[bool],
    no_cache: bool,
    cache_only: bool,
):
    """View a Reddit user's profile or activity.

    \b
    Examples:
        fcrawl reddit user spez
        fcrawl reddit user spez --posts-only --sort top
        fcrawl reddit user u/spez --comments-only -l 10
    """
    pretty = resolve_pretty(pretty)

    if posts_only and comments_only:
        raise click.BadParameter("cannot use --posts-only and --comments-only together")
    if limit < 1:
        raise click.BadParameter("limit must be >= 1", param_hint="--limit")

    limit = min(limit, 100)
    user_name = _normalize_username(username)
    client = RedditClient()

    if posts_only:
        activity_type = "submitted"
    elif comments_only:
        activity_type = "comments"

    profile_data = None
    profile_from_cache = False

    if about:
        profile_data, profile_from_cache = _fetch_with_cache(
            client=client,
            cache_bucket="reddit-user-about",
            path=f"user/{user_name}/about",
            params={},
            no_cache=no_cache,
            cache_only=cache_only,
            progress_label=f"Fetching u/{user_name} profile...",
        )

        payload = user_to_dict(profile_data)
        if json_output or output:
            output_data = {
                "profile": payload,
                "meta": {"from_cache": profile_from_cache},
            }
            _print_json_or_save(output_data, output, json_output, pretty)
            return

        display_user_about(profile_data, pretty=pretty)
        return

    if activity_type == "overview":
        profile_data, profile_from_cache = _fetch_with_cache(
            client=client,
            cache_bucket="reddit-user-about",
            path=f"user/{user_name}/about",
            params={},
            no_cache=no_cache,
            cache_only=cache_only,
            progress_label=f"Fetching u/{user_name} profile...",
        )

    activity_params: dict[str, Any] = {
        "sort": sort,
        "t": time_filter,
        "limit": limit,
    }
    if after:
        activity_params["after"] = after

    activity_data, activity_from_cache = _fetch_with_cache(
        client=client,
        cache_bucket="reddit-user-activity",
        path=f"user/{user_name}/{activity_type}",
        params=activity_params,
        no_cache=no_cache,
        cache_only=cache_only,
        progress_label=f"Fetching u/{user_name} activity...",
    )

    listing = activity_data.get("data", {})
    items = listing.get("children", [])
    next_after = listing.get("after")
    if not items:
        console.print(f"[yellow]No activity found for u/{user_name}[/yellow]")
        return

    console.print(f"[green]Found {len(items)} items from u/{user_name}[/green]")

    if json_output or output:
        output_data = {
            "username": user_name,
            "type": activity_type,
            "sort": sort,
            "time": time_filter,
            "profile": user_to_dict(profile_data) if profile_data else None,
            "activity": [
                item
                for item in (activity_item_to_dict(activity) for activity in items)
                if item is not None
            ],
            "meta": {
                "count": len(items),
                "next_after": next_after,
                "from_cache": {
                    "profile": profile_from_cache,
                    "activity": activity_from_cache,
                },
            },
        }
        _print_json_or_save(output_data, output, json_output, pretty)
        return

    if profile_data is not None:
        display_user_about(profile_data, pretty=pretty)
        if pretty:
            console.print("[bold]Recent Activity[/bold]")
            console.print("-" * 40)
        else:
            print("Recent Activity")
            print("-" * 40)

    display_user_activity(items, pretty=pretty)

    if next_after:
        cmd = (
            f"fcrawl reddit user {shlex.quote(user_name)} --type {activity_type} "
            f"--sort {sort} --time {time_filter} -l {limit}"
        )
        _print_next_after_hint(cmd, next_after, pretty)
