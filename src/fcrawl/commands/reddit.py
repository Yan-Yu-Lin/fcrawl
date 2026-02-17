"""Reddit commands for fcrawl.

Read-only access to Reddit via public .json endpoints.
No authentication or API keys required.
"""

import json
import re
from datetime import datetime, timezone
from typing import Optional

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.output import handle_output, save_to_file
from ..utils.reddit_client import RedditClient

console = Console()


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


def format_timestamp(utc: float | int) -> str:
    """Convert Unix timestamp to human-readable relative time."""
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


def format_date(utc: float | int) -> str:
    """Convert Unix timestamp to date string."""
    dt = datetime.fromtimestamp(utc, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d")


def extract_permalink(url: str) -> str:
    """Extract Reddit permalink path from various URL formats.

    Handles:
      - https://www.reddit.com/r/sub/comments/id/slug/
      - https://old.reddit.com/r/sub/comments/id/slug/
      - https://reddit.com/r/sub/comments/id/slug/
      - /r/sub/comments/id/slug/   (already a path)
    """
    # Strip query params and fragment
    url = url.split("?")[0].split("#")[0]

    # If it's already a path starting with /r/
    if url.startswith("/r/"):
        return url.rstrip("/")

    # Extract path from full URL
    match = re.search(r"reddit\.com(/r/\S+)", url)
    if match:
        return match.group(1).rstrip("/")

    raise ValueError(f"Cannot parse Reddit URL: {url}")


def _truncate(text: str, length: int = 80) -> str:
    """Truncate text to length, adding ellipsis if needed."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= length:
        return text
    return text[: length - 1] + "\u2026"


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------

def display_post_table(posts: list[dict]):
    """Display a list of posts as a Rich table (for search/subreddit feed)."""
    table = Table(show_header=True, header_style="bold")
    table.add_column("Sub", style="cyan", max_width=18)
    table.add_column("Score", justify="right", style="green", max_width=7)
    table.add_column("Cmt", justify="right", style="yellow", max_width=5)
    table.add_column("Age", style="dim", max_width=7)
    table.add_column("Title")

    for p in posts:
        data = p.get("data", p)
        sub = data.get("subreddit", "")
        score = format_number(data.get("score", 0))
        comments = format_number(data.get("num_comments", 0))
        age = format_timestamp(data.get("created_utc", 0))
        title = _truncate(data.get("title", ""), 70)
        flair = data.get("link_flair_text")
        if flair:
            title = f"[{flair}] {title}"

        table.add_row(f"r/{sub}", score, comments, age, title)

    console.print(table)


def display_post(post_data: dict, show_body: bool = True):
    """Display a single post in formatted blocks."""
    data = post_data.get("data", post_data)
    title = data.get("title", "")
    author = data.get("author", "[deleted]")
    subreddit = data.get("subreddit", "")
    score = format_number(data.get("score", 0))
    comments_count = format_number(data.get("num_comments", 0))
    age = format_timestamp(data.get("created_utc", 0))
    flair = data.get("link_flair_text")
    url = data.get("url", "")
    selftext = data.get("selftext", "")
    permalink = data.get("permalink", "")

    console.print("=" * 60)
    console.print(f"[bold]{title}[/bold]")
    if flair:
        console.print(f"[magenta][{flair}][/magenta]")
    console.print(
        f"[cyan]r/{subreddit}[/cyan] | "
        f"[dim]u/{author}[/dim] | "
        f"[green]{score} pts[/green] | "
        f"[yellow]{comments_count} comments[/yellow] | "
        f"[dim]{age}[/dim]"
    )
    if url and not url.startswith(f"https://www.reddit.com{permalink}"):
        # External link post
        console.print(f"[blue]{url}[/blue]")
    console.print(f"[dim]https://www.reddit.com{permalink}[/dim]")

    if show_body and selftext:
        console.print()
        console.print(selftext)

    console.print()


def display_comment_tree(children: list, depth: int = 0, max_depth: int = 3):
    """Recursively display a comment tree with indentation."""
    for child in children:
        if child.get("kind") != "t1":
            # "more" stubs or other types — skip
            continue
        data = child.get("data", {})
        author = data.get("author", "[deleted]")
        score = data.get("score", 0)
        body = data.get("body", "")
        age = format_timestamp(data.get("created_utc", 0))

        indent = "  " * depth
        bar = "[dim]|[/dim] " * depth

        # Author line
        console.print(
            f"{bar}[bold cyan]u/{author}[/bold cyan] "
            f"[green]({format_number(score)} pts)[/green] "
            f"[dim]{age}[/dim]"
        )
        # Body — indent each line
        if body and body != "[deleted]":
            for line in body.split("\n"):
                if line.strip():
                    console.print(f"{bar}  {line}")
        console.print(f"{bar}")

        # Recurse into replies
        if depth < max_depth - 1:
            replies = data.get("replies")
            if replies and isinstance(replies, dict):
                reply_children = (
                    replies.get("data", {}).get("children", [])
                )
                if reply_children:
                    display_comment_tree(
                        reply_children, depth + 1, max_depth
                    )


def display_subreddit_about(data: dict):
    """Display subreddit info."""
    d = data.get("data", data)
    name = d.get("display_name", "")
    title = d.get("title", "")
    desc = d.get("public_description", "") or d.get("description", "")
    subscribers = format_number(d.get("subscribers", 0))
    active = format_number(d.get("accounts_active", 0))
    created = format_date(d.get("created_utc", 0))
    nsfw = d.get("over18", False)

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


def display_user_about(data: dict):
    """Display user profile info."""
    d = data.get("data", data)
    name = d.get("name", "")
    comment_karma = format_number(d.get("comment_karma", 0))
    link_karma = format_number(d.get("link_karma", 0))
    total_karma = format_number(d.get("total_karma", 0))
    created = format_date(d.get("created_utc", 0))
    desc = d.get("subreddit", {}).get("public_description", "") if isinstance(d.get("subreddit"), dict) else ""
    is_gold = d.get("is_gold", False)
    verified = d.get("verified", False)

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


def display_user_activity(items: list):
    """Display a user's mixed activity (posts + comments)."""
    for item in items:
        kind = item.get("kind", "")
        data = item.get("data", {})

        if kind == "t3":
            # Post
            sub = data.get("subreddit", "")
            title = _truncate(data.get("title", ""), 70)
            score = format_number(data.get("score", 0))
            age = format_timestamp(data.get("created_utc", 0))
            console.print(
                f"[bold blue][post][/bold blue] "
                f"[cyan]r/{sub}[/cyan] "
                f"[green]{score} pts[/green] "
                f"[dim]{age}[/dim]"
            )
            console.print(f"  {title}")
        elif kind == "t1":
            # Comment
            sub = data.get("subreddit", "")
            body = _truncate(data.get("body", ""), 100)
            score = format_number(data.get("score", 0))
            age = format_timestamp(data.get("created_utc", 0))
            link_title = _truncate(data.get("link_title", ""), 50)
            console.print(
                f"[bold yellow][comment][/bold yellow] "
                f"[cyan]r/{sub}[/cyan] "
                f"[green]{score} pts[/green] "
                f"[dim]{age}[/dim]"
            )
            if link_title:
                console.print(f"  [dim]on: {link_title}[/dim]")
            console.print(f"  {body}")
        else:
            continue
        console.print()


# ---------------------------------------------------------------------------
# JSON serializers
# ---------------------------------------------------------------------------

def post_to_dict(data: dict) -> dict:
    """Convert a Reddit post data blob to a clean dict for JSON output."""
    d = data.get("data", data)
    return {
        "id": d.get("id"),
        "title": d.get("title"),
        "author": d.get("author"),
        "subreddit": d.get("subreddit"),
        "score": d.get("score"),
        "num_comments": d.get("num_comments"),
        "url": d.get("url"),
        "permalink": f"https://www.reddit.com{d.get('permalink', '')}",
        "selftext": d.get("selftext"),
        "created_utc": d.get("created_utc"),
        "flair": d.get("link_flair_text"),
    }


def comment_to_dict(child: dict, max_depth: int = 3, depth: int = 0) -> Optional[dict]:
    """Convert a Reddit comment to a clean dict, recursively including replies."""
    if child.get("kind") != "t1":
        return None
    data = child.get("data", {})
    result = {
        "author": data.get("author"),
        "score": data.get("score"),
        "body": data.get("body"),
        "created_utc": data.get("created_utc"),
    }
    if depth < max_depth - 1:
        replies = data.get("replies")
        if replies and isinstance(replies, dict):
            reply_children = replies.get("data", {}).get("children", [])
            result["replies"] = [
                c
                for c in (comment_to_dict(r, max_depth, depth + 1) for r in reply_children)
                if c is not None
            ]
    return result


def user_to_dict(data: dict) -> dict:
    """Convert user about data to a clean dict."""
    d = data.get("data", data)
    return {
        "name": d.get("name"),
        "total_karma": d.get("total_karma"),
        "link_karma": d.get("link_karma"),
        "comment_karma": d.get("comment_karma"),
        "created_utc": d.get("created_utc"),
        "is_gold": d.get("is_gold"),
        "verified": d.get("verified"),
    }


def subreddit_to_dict(data: dict) -> dict:
    """Convert subreddit about data to a clean dict."""
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
        fcrawl reddit subreddit python -l 20
        fcrawl reddit user spez --about
    """
    pass


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

@reddit.command(name="search")
@click.argument("query")
@click.option("--subreddit", "-s", default=None, help="Restrict search to a subreddit")
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
@click.option("--limit", "-l", type=int, default=25, help="Max results (default: 25, max: 100)")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def reddit_search(
    query: str,
    subreddit: Optional[str],
    sort: str,
    time_filter: str,
    limit: int,
    output: Optional[str],
    json_output: bool,
):
    """Search Reddit posts.

    \b
    Examples:
        fcrawl reddit search "python async"
        fcrawl reddit search "hooks" -s ClaudeCode --sort top
        fcrawl reddit search "rust vs go" --time week -l 10
    """
    limit = min(limit, 100)
    client = RedditClient()

    if subreddit:
        path = f"r/{subreddit}/search"
        params = {
            "q": query,
            "restrict_sr": "on",
            "sort": sort,
            "t": time_filter,
            "limit": limit,
        }
    else:
        path = "search"
        params = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": limit,
        }

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        desc = f"Searching Reddit for '{query}'"
        if subreddit:
            desc += f" in r/{subreddit}"
        progress.add_task(f"{desc}...", total=None)

        try:
            result = client.get(path, params)
            progress.stop()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    posts = result.get("data", {}).get("children", [])
    if not posts:
        console.print("[yellow]No results found[/yellow]")
        return

    console.print(f"[green]Found {len(posts)} results[/green]")

    if json_output or output:
        data = [post_to_dict(p) for p in posts]
        if output:
            save_to_file(json.dumps(data, indent=2), output, "json")
        if json_output and not output:
            console.print_json(json.dumps(data, indent=2))
    else:
        display_post_table(posts)


# ---------------------------------------------------------------------------
# post
# ---------------------------------------------------------------------------

@reddit.command(name="post")
@click.argument("url")
@click.option("--comments", "-c", type=int, default=10, help="Max top-level comments (default: 10, 0=all)")
@click.option("--depth", "-d", type=int, default=3, help="Comment nesting depth (default: 3)")
@click.option(
    "--sort",
    type=click.Choice(["best", "top", "new", "controversial", "old", "qa"]),
    default="best",
    help="Comment sort order (default: best)",
)
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def reddit_post(
    url: str,
    comments: int,
    depth: int,
    sort: str,
    output: Optional[str],
    json_output: bool,
):
    """Fetch a Reddit post with comments.

    \b
    Examples:
        fcrawl reddit post https://reddit.com/r/python/comments/abc123/my_post/
        fcrawl reddit post https://reddit.com/r/sub/comments/id/slug/ -c 5 --depth 2
        fcrawl reddit post URL --sort top --json
    """
    try:
        permalink = extract_permalink(url)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise click.Abort()

    client = RedditClient()
    params = {"sort": sort, "depth": depth}
    if comments > 0:
        params["limit"] = comments

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        progress.add_task("Fetching post...", total=None)

        try:
            result = client.get(permalink, params)
            progress.stop()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    # Reddit returns a list: [post_listing, comments_listing]
    if not isinstance(result, list) or len(result) < 2:
        console.print("[red]Unexpected response format[/red]")
        raise click.Abort()

    post_children = result[0].get("data", {}).get("children", [])
    comment_children = result[1].get("data", {}).get("children", [])

    if not post_children:
        console.print("[yellow]Post not found[/yellow]")
        return

    post = post_children[0]

    if json_output or output:
        data = {
            "post": post_to_dict(post),
            "comments": [
                c
                for c in (comment_to_dict(ch, depth) for ch in comment_children)
                if c is not None
            ],
        }
        if output:
            save_to_file(json.dumps(data, indent=2), output, "json")
        if json_output and not output:
            console.print_json(json.dumps(data, indent=2))
    else:
        display_post(post)
        if comment_children:
            console.print("[bold]Comments[/bold]")
            console.print("-" * 40)
            display_comment_tree(comment_children, depth=0, max_depth=depth)


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
@click.option("--limit", "-l", type=int, default=25, help="Max posts (default: 25, max: 100)")
@click.option("--about", is_flag=True, help="Show subreddit info instead of feed")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def reddit_subreddit(
    name: str,
    sort: str,
    time_filter: str,
    limit: int,
    about: bool,
    output: Optional[str],
    json_output: bool,
):
    """Browse a subreddit's feed or info.

    \b
    Examples:
        fcrawl reddit subreddit python
        fcrawl reddit subreddit ClaudeCode --sort top --time week
        fcrawl reddit subreddit python --about
    """
    # Strip r/ prefix if user included it
    name = name.removeprefix("r/")
    limit = min(limit, 100)
    client = RedditClient()

    if about:
        path = f"r/{name}/about"
        params = {}
    else:
        path = f"r/{name}/{sort}"
        params = {"t": time_filter, "limit": limit}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        desc = f"Fetching r/{name} info..." if about else f"Fetching r/{name}/{sort}..."
        progress.add_task(desc, total=None)

        try:
            result = client.get(path, params)
            progress.stop()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    if about:
        if json_output or output:
            data = subreddit_to_dict(result)
            if output:
                save_to_file(json.dumps(data, indent=2), output, "json")
            if json_output and not output:
                console.print_json(json.dumps(data, indent=2))
        else:
            display_subreddit_about(result)
    else:
        posts = result.get("data", {}).get("children", [])
        if not posts:
            console.print(f"[yellow]No posts found in r/{name}[/yellow]")
            return

        console.print(f"[green]Found {len(posts)} posts in r/{name}[/green]")

        if json_output or output:
            data = [post_to_dict(p) for p in posts]
            if output:
                save_to_file(json.dumps(data, indent=2), output, "json")
            if json_output and not output:
                console.print_json(json.dumps(data, indent=2))
        else:
            display_post_table(posts)


# ---------------------------------------------------------------------------
# user
# ---------------------------------------------------------------------------

@reddit.command(name="user")
@click.argument("username")
@click.option("--about", is_flag=True, help="Show profile info instead of activity")
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
@click.option("--limit", "-l", type=int, default=25, help="Max items (default: 25, max: 100)")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
def reddit_user(
    username: str,
    about: bool,
    activity_type: str,
    sort: str,
    time_filter: str,
    limit: int,
    output: Optional[str],
    json_output: bool,
):
    """View a Reddit user's profile or activity.

    \b
    Examples:
        fcrawl reddit user spez
        fcrawl reddit user spez --about
        fcrawl reddit user spez --type submitted --sort top
    """
    # Strip u/ prefix if user included it
    username = username.removeprefix("u/")
    limit = min(limit, 100)
    client = RedditClient()

    if about:
        path = f"user/{username}/about"
        params = {}
    else:
        path = f"user/{username}/{activity_type}"
        params = {"sort": sort, "t": time_filter, "limit": limit}

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
    ) as progress:
        desc = f"Fetching u/{username} profile..." if about else f"Fetching u/{username} activity..."
        progress.add_task(desc, total=None)

        try:
            result = client.get(path, params)
            progress.stop()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    if about:
        if json_output or output:
            data = user_to_dict(result)
            if output:
                save_to_file(json.dumps(data, indent=2), output, "json")
            if json_output and not output:
                console.print_json(json.dumps(data, indent=2))
        else:
            display_user_about(result)
    else:
        items = result.get("data", {}).get("children", [])
        if not items:
            console.print(f"[yellow]No activity found for u/{username}[/yellow]")
            return

        console.print(f"[green]Found {len(items)} items from u/{username}[/green]")

        if json_output or output:
            data = []
            for item in items:
                kind = item.get("kind", "")
                if kind == "t3":
                    data.append({"type": "post", **post_to_dict(item)})
                elif kind == "t1":
                    data.append({"type": "comment", **comment_to_dict(item, max_depth=1)})
            if output:
                save_to_file(json.dumps(data, indent=2), output, "json")
            if json_output and not output:
                console.print_json(json.dumps(data, indent=2))
        else:
            display_user_activity(items)
