"""X/Twitter commands for fcrawl"""

import asyncio
import json
import re
from contextlib import aclosing
from typing import Optional

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

from ..utils.article_parser import parse_article_from_response
from ..utils.output import handle_output, save_to_file
from ..utils.x_client import get_x_api, get_x_db_path, get_x_pool
from ..vendors.twscrape import NoAccountError, Tweet, User
from ..vendors.twscrape.models import parse_tweet

console = Console()


def extract_tweet_id(id_or_url: str) -> int:
    """Extract tweet ID from URL or return the ID directly."""
    # Handle URLs like https://x.com/user/status/123456789 or https://twitter.com/user/status/123456789
    match = re.search(r'/status/(\d+)', id_or_url)
    if match:
        return int(match.group(1))
    # Assume it's a direct ID
    return int(id_or_url)


def format_number(n: int | None) -> str:
    """Format a number with commas for readability."""
    if n is None:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def display_tweet(tweet: Tweet):
    """Display a tweet in a formatted way."""
    console.print("=" * 60)
    console.print(f"[bold cyan]@{tweet.user.username}[/bold cyan] ({tweet.user.displayname})")
    console.print(f"[dim]Followers: {format_number(tweet.user.followersCount)} | {tweet.date.strftime('%Y-%m-%d %H:%M:%S')}[/dim]")
    console.print(f"[blue]{tweet.url}[/blue]")
    console.print()
    console.print(tweet.rawContent)
    console.print()

    # Engagement stats
    stats = []
    stats.append(f"[red]<3[/red] {format_number(tweet.likeCount)}")
    stats.append(f"[green]RT[/green] {format_number(tweet.retweetCount)}")
    stats.append(f"[blue]Reply[/blue] {format_number(tweet.replyCount)}")
    if tweet.viewCount:
        stats.append(f"[dim]Views[/dim] {format_number(tweet.viewCount)}")
    console.print("  ".join(stats))


def display_article(article):
    """Display article content in markdown format."""
    console.print()
    console.print("─" * 60)
    console.print(f"[bold magenta]📄 Article: {article.title}[/bold magenta]")
    console.print("─" * 60)
    console.print()
    console.print(article.to_markdown())
    console.print()
    if article.cover_image_url:
        console.print(f"[dim]Cover image: {article.cover_image_url}[/dim]")


def display_user(user: User):
    """Display a user profile in a formatted way."""
    console.print("=" * 60)
    console.print(f"[bold cyan]@{user.username}[/bold cyan] ({user.displayname})")
    if user.blue:
        console.print("[blue]Verified[/blue]")
    console.print(f"[blue]{user.url}[/blue]")
    console.print()
    if user.rawDescription:
        console.print(user.rawDescription)
        console.print()
    if user.location:
        console.print(f"[dim]Location:[/dim] {user.location}")
    console.print(f"[dim]Joined:[/dim] {user.created.strftime('%Y-%m-%d')}")
    console.print()
    console.print(f"[bold]Followers:[/bold] {format_number(user.followersCount)}  [bold]Following:[/bold] {format_number(user.friendsCount)}")
    console.print(f"[bold]Tweets:[/bold] {format_number(user.statusesCount)}  [bold]Likes:[/bold] {format_number(user.favouritesCount)}")


def tweet_to_dict(tweet: Tweet) -> dict:
    """Convert a Tweet to a dictionary for JSON output."""
    return {
        "id": tweet.id,
        "url": tweet.url,
        "date": tweet.date.isoformat(),
        "user": {
            "username": tweet.user.username,
            "displayname": tweet.user.displayname,
            "followersCount": tweet.user.followersCount,
        },
        "content": tweet.rawContent,
        "likeCount": tweet.likeCount,
        "retweetCount": tweet.retweetCount,
        "replyCount": tweet.replyCount,
        "viewCount": tweet.viewCount,
        "hashtags": tweet.hashtags,
    }


def user_to_dict(user: User) -> dict:
    """Convert a User to a dictionary for JSON output."""
    return {
        "id": user.id,
        "username": user.username,
        "displayname": user.displayname,
        "url": user.url,
        "description": user.rawDescription,
        "location": user.location,
        "created": user.created.isoformat(),
        "followersCount": user.followersCount,
        "friendsCount": user.friendsCount,
        "statusesCount": user.statusesCount,
        "favouritesCount": user.favouritesCount,
        "verified": user.verified,
        "blue": user.blue,
    }


@click.group()
def x():
    """X/Twitter commands - search, fetch tweets, and manage accounts.

    \b
    Examples:
        fcrawl x search "Claude Code" --limit 10
        fcrawl x tweet https://x.com/user/status/123
        fcrawl x user elonmusk
        fcrawl x tweets anthropic --limit 20
        fcrawl x accounts
    """
    pass


@x.command(name='search')
@click.argument('query')
@click.option('--limit', '-l', type=int, default=20, help='Maximum number of results')
@click.option('--sort', type=click.Choice(['top', 'latest', 'photos', 'videos']), default='top',
              help='Sort order (default: top)')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def x_search(query: str, limit: int, sort: str, output: Optional[str], json_output: bool):
    """Search for tweets on X/Twitter.

    \b
    Examples:
        fcrawl x search "Claude Code"
        fcrawl x search "AI news" --limit 50 --sort top
        fcrawl x search "python" -o results.json --json
    """
    # Map sort options to X API product values
    sort_map = {
        'top': 'Top',
        'latest': 'Latest',
        'photos': 'Photos',
        'videos': 'Videos',
    }
    product = sort_map.get(sort, 'Latest')

    async def _fetch():
        api = get_x_api()
        results = []
        async with aclosing(api.search(query, limit=limit, kv={'product': product})) as gen:
            async for tweet in gen:
                results.append(tweet)
                if len(results) >= limit:
                    break
        return results

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Searching for '{query}'...", total=None)

        try:
            tweets = asyncio.run(_fetch())
            progress.stop()
        except NoAccountError:
            progress.stop()
            console.print("[red]No X accounts configured.[/red]")
            console.print("Run: [bold]fcrawl x accounts add <file>[/bold]")
            console.print("\nFile format: username:password:email:email_password (one per line)")
            raise click.Abort()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    if not tweets:
        console.print("[yellow]No tweets found[/yellow]")
        return

    console.print(f"[green]Found {len(tweets)} tweets[/green]")

    if json_output or output:
        data = [tweet_to_dict(t) for t in tweets]
        if output:
            save_to_file(json.dumps(data, indent=2), output, 'json')
        if json_output and not output:
            console.print_json(json.dumps(data, indent=2))
    else:
        for tweet in tweets:
            display_tweet(tweet)
        console.print("=" * 60)


@x.command(name='tweet')
@click.argument('id_or_url')
@click.option('--thread', is_flag=True, help='Fetch full thread (author replies)')
@click.option('--with-replies', is_flag=True, help='Include replies from other users (use with --thread)')
@click.option('--reply-limit', type=int, default=30, help='Max replies to fetch (default: 30)')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def x_tweet(id_or_url: str, thread: bool, with_replies: bool, reply_limit: int, output: Optional[str], json_output: bool):
    """Fetch a single tweet by ID or URL.

    \b
    Examples:
        fcrawl x tweet 1234567890
        fcrawl x tweet https://x.com/user/status/1234567890
        fcrawl x tweet 1234567890 --thread  # Fetch full thread
        fcrawl x tweet 1234567890 --thread --with-replies  # Thread + other replies
    """
    try:
        tweet_id = extract_tweet_id(id_or_url)
    except ValueError:
        console.print(f"[red]Invalid tweet ID or URL: {id_or_url}[/red]")
        raise click.Abort()

    async def _fetch():
        api = get_x_api()
        # Use raw to get both tweet and article data
        rep = await api.tweet_details_raw(tweet_id)
        if not rep:
            return None, None
        response_json = rep.json()
        tweet = parse_tweet(rep, tweet_id)
        article = parse_article_from_response(response_json)
        return tweet, article

    def _is_reply_to_others(tweet, author_username: str) -> bool:
        """Check if tweet is an @reply to someone other than self."""
        content = tweet.rawContent.strip()
        # If starts with @mention that isn't the author, it's a reply to others
        if content.startswith('@'):
            # Extract the first @mention
            import re
            match = re.match(r'@(\w+)', content)
            if match:
                mentioned = match.group(1).lower()
                # If mentioning someone other than self, it's a reply to others
                if mentioned != author_username.lower():
                    return True
        return False

    async def _fetch_thread(original_tweet, with_replies: bool = False, reply_limit: int = 30):
        """Fetch full thread by walking the inReplyToTweetId chain + fetching replies."""
        api = get_x_api()
        author_id = original_tweet.user.id
        author_username = original_tweet.user.username
        conv_id = original_tweet.conversationId if hasattr(original_tweet, 'conversationId') else original_tweet.id

        seen_ids = set()
        thread_tweets = {}
        other_replies = {}  # Collect replies from other users

        async def add_tweet(t):
            """Add tweet to thread if it's from the author and not a reply to others."""
            if t.id in seen_ids:
                return
            if t.user.id != author_id:
                return
            if _is_reply_to_others(t, author_username):
                return
            seen_ids.add(t.id)
            thread_tweets[t.id] = t

        async def walk_chain_backwards(start_id):
            """Walk backwards via inReplyToTweetId to find all parent tweets."""
            current_id = start_id
            while current_id and current_id not in seen_ids:
                t = await api.tweet_details(current_id)
                if not t:
                    break
                if t.user.id != author_id:
                    break  # Hit a tweet from another user, stop
                await add_tweet(t)
                current_id = t.inReplyToTweetId

        async def walk_chain_forwards(start_id):
            """Walk forwards by fetching replies and following author's replies."""
            to_check = [start_id]
            checked = set()
            while to_check:
                current_id = to_check.pop(0)
                if current_id in checked:
                    continue
                checked.add(current_id)

                async with aclosing(api.tweet_replies(current_id, limit=50)) as gen:
                    async for reply in gen:
                        if reply.id in seen_ids:
                            continue
                        if reply.user.id == author_id:
                            await add_tweet(reply)
                            to_check.append(reply.id)
                        elif with_replies and len(other_replies) < reply_limit:
                            # Collect replies from other users
                            seen_ids.add(reply.id)
                            other_replies[reply.id] = reply

        # Step 1: Add original tweet
        await add_tweet(original_tweet)

        # Step 2: Get tweets from user timeline with same conversationId
        # Lower limit since old threads won't be found anyway, forwards walking handles it
        async with aclosing(api.user_tweets_and_replies(author_id, limit=50)) as gen:
            async for t in gen:
                if hasattr(t, 'conversationId') and t.conversationId == conv_id:
                    await add_tweet(t)

        # Step 3: Walk backwards from each found tweet to fill gaps
        for tid in list(thread_tweets.keys()):
            t = thread_tweets[tid]
            if t.inReplyToTweetId:
                await walk_chain_backwards(t.inReplyToTweetId)

        # Step 4: Walk forwards from original to find any replies we missed
        # Only do this if we need replies from others, or if timeline didn't find the thread
        if with_replies or len(thread_tweets) <= 1:
            await walk_chain_forwards(original_tweet.id)

        # If we didn't find anything, fall back to just the original
        if not thread_tweets:
            thread_tweets[original_tweet.id] = original_tweet

        # Sort thread by tweet ID (chronological order)
        thread_result = list(thread_tweets.values())
        thread_result.sort(key=lambda t: t.id)

        # Sort other replies by likes (popularity)
        replies_result = list(other_replies.values())
        replies_result.sort(key=lambda t: t.likeCount, reverse=True)

        return thread_result, replies_result

    # Validate: --with-replies requires --thread
    if with_replies and not thread:
        console.print("[yellow]--with-replies requires --thread flag[/yellow]")
        with_replies = False

    replies = []  # Other users' replies
    article = None  # Article if present

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        desc = f"Fetching thread {tweet_id}..." if thread else f"Fetching tweet {tweet_id}..."
        if with_replies:
            desc = f"Fetching thread + replies {tweet_id}..."
        task = progress.add_task(desc, total=None)

        try:
            tweet, article = asyncio.run(_fetch())
            if not tweet:
                progress.stop()
                console.print(f"[yellow]Tweet {tweet_id} not found[/yellow]")
                return

            if thread:
                tweets, replies = asyncio.run(_fetch_thread(tweet, with_replies, reply_limit))
                progress.stop()
                msg = f"[green]Found {len(tweets)} tweets in thread[/green]"
                if replies:
                    msg += f" [dim]+ {len(replies)} replies[/dim]"
                console.print(msg)
            else:
                tweets = [tweet]
                progress.stop()

        except NoAccountError:
            progress.stop()
            console.print("[red]No X accounts configured.[/red]")
            console.print("Run: [bold]fcrawl x accounts add <file>[/bold]")
            raise click.Abort()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    if json_output or output:
        if thread and with_replies:
            # Structured output with thread and replies
            data = {
                "thread": [tweet_to_dict(t) for t in tweets],
                "replies": [tweet_to_dict(t) for t in replies],
            }
            if article:
                data["article"] = article.to_dict()
        elif thread:
            data = [tweet_to_dict(t) for t in tweets]
            if article:
                data = {"thread": data, "article": article.to_dict()}
        else:
            data = tweet_to_dict(tweets[0])
            if article:
                data = {"tweet": data, "article": article.to_dict()}
        if output:
            save_to_file(json.dumps(data, indent=2), output, 'json')
        if json_output and not output:
            console.print_json(json.dumps(data, indent=2))
    else:
        # Display thread
        for t in tweets:
            display_tweet(t)
        if thread and len(tweets) > 1:
            console.print(f"\n[dim]Thread: {len(tweets)} tweets from @{tweets[0].user.username}[/dim]")

        # Display article if present
        if article:
            display_article(article)

        # Display replies if any
        if replies:
            console.print("\n" + "─" * 60)
            console.print(f"[bold]Replies[/bold] [dim]({len(replies)} sorted by likes)[/dim]")
            console.print("─" * 60)
            for t in replies:
                display_tweet(t)


def extract_article_tweet_id(id_or_url: str) -> int:
    """Extract tweet ID from tweet URL, article URL, or direct ID.

    Handles:
    - https://x.com/user/status/123456789
    - https://x.com/i/article/123456789
    - 123456789 (direct ID)
    """
    # Handle article URLs like https://x.com/i/article/123456789
    article_match = re.search(r'/i/article/(\d+)', id_or_url)
    if article_match:
        # Article ID - need to find parent tweet
        # For now, return the article ID and handle specially
        return int(article_match.group(1))

    # Handle tweet URLs
    status_match = re.search(r'/status/(\d+)', id_or_url)
    if status_match:
        return int(status_match.group(1))

    # Assume it's a direct ID
    return int(id_or_url)


@x.command(name='article')
@click.argument('id_or_url')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
@click.option('--raw', is_flag=True, help='Output raw Draft.js blocks instead of markdown')
def x_article(id_or_url: str, output: Optional[str], json_output: bool, raw: bool):
    """Fetch an X article by tweet ID/URL.

    \b
    Examples:
        fcrawl x article https://x.com/user/status/123
        fcrawl x article 123456789
        fcrawl x article 123 -o article.md
        fcrawl x article 123 --json
    """
    from ..utils.article_parser import parse_article_from_response

    try:
        tweet_id = extract_article_tweet_id(id_or_url)
    except ValueError:
        console.print(f"[red]Invalid tweet/article ID or URL: {id_or_url}[/red]")
        raise click.Abort()

    async def _fetch():
        api = get_x_api()
        return await api.tweet_with_article_raw(tweet_id)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Fetching article from tweet {tweet_id}...", total=None)

        try:
            rep = asyncio.run(_fetch())
            progress.stop()
        except NoAccountError:
            progress.stop()
            console.print("[red]No X accounts configured.[/red]")
            console.print("Run: [bold]fcrawl x accounts add <file>[/bold]")
            raise click.Abort()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    if not rep:
        console.print(f"[yellow]Tweet {tweet_id} not found[/yellow]")
        return

    # Parse article from response
    response_json = rep.json()
    article = parse_article_from_response(response_json)

    if not article:
        console.print(f"[yellow]No article found in tweet {tweet_id}[/yellow]")
        return

    console.print(f"[green]Found article: {article.title}[/green]")

    if json_output or output:
        if raw:
            # Output raw blocks
            data = {
                "rest_id": article.rest_id,
                "title": article.title,
                "preview_text": article.preview_text,
                "cover_image_url": article.cover_image_url,
                "content_blocks": article.content_blocks,
            }
        else:
            data = article.to_dict()

        if output:
            if output.endswith('.json') or json_output:
                save_to_file(json.dumps(data, indent=2), output, 'json')
            else:
                # Save as markdown
                save_to_file(article.to_markdown(), output, 'md')
        if json_output and not output:
            console.print_json(json.dumps(data, indent=2))
    else:
        # Display as markdown
        markdown_content = article.to_markdown()
        console.print()
        console.print(markdown_content)
        console.print()
        if article.cover_image_url:
            console.print(f"[dim]Cover image: {article.cover_image_url}[/dim]")


@x.command(name='user')
@click.argument('handle')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def x_user(handle: str, output: Optional[str], json_output: bool):
    """Fetch a user profile by handle.

    \b
    Examples:
        fcrawl x user elonmusk
        fcrawl x user anthropic --json
    """
    # Remove @ if present
    handle = handle.lstrip('@')

    async def _fetch():
        api = get_x_api()
        return await api.user_by_login(handle)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Fetching user @{handle}...", total=None)

        try:
            user = asyncio.run(_fetch())
            progress.stop()
        except NoAccountError:
            progress.stop()
            console.print("[red]No X accounts configured.[/red]")
            console.print("Run: [bold]fcrawl x accounts add <file>[/bold]")
            raise click.Abort()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    if not user:
        console.print(f"[yellow]User @{handle} not found[/yellow]")
        return

    if json_output or output:
        data = user_to_dict(user)
        if output:
            save_to_file(json.dumps(data, indent=2), output, 'json')
        if json_output and not output:
            console.print_json(json.dumps(data, indent=2))
    else:
        display_user(user)


@x.command(name='tweets')
@click.argument('handle')
@click.option('--limit', '-l', type=int, default=20, help='Maximum number of tweets')
@click.option('-o', '--output', help='Save output to file')
@click.option('--json', 'json_output', is_flag=True, help='Output as JSON')
def x_tweets(handle: str, limit: int, output: Optional[str], json_output: bool):
    """Fetch tweets from a user's timeline.

    \b
    Examples:
        fcrawl x tweets anthropic
        fcrawl x tweets elonmusk --limit 50
    """
    # Remove @ if present
    handle = handle.lstrip('@')

    async def _fetch():
        api = get_x_api()
        # First get the user to get their ID
        user = await api.user_by_login(handle)
        if not user:
            return None, []

        results = []
        async with aclosing(api.user_tweets(user.id, limit=limit)) as gen:
            async for tweet in gen:
                results.append(tweet)
                if len(results) >= limit:
                    break
        return user, results

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task(f"Fetching tweets from @{handle}...", total=None)

        try:
            user, tweets = asyncio.run(_fetch())
            progress.stop()
        except NoAccountError:
            progress.stop()
            console.print("[red]No X accounts configured.[/red]")
            console.print("Run: [bold]fcrawl x accounts add <file>[/bold]")
            raise click.Abort()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    if user is None:
        console.print(f"[yellow]User @{handle} not found[/yellow]")
        return

    if not tweets:
        console.print(f"[yellow]No tweets found for @{handle}[/yellow]")
        return

    console.print(f"[green]Found {len(tweets)} tweets from @{handle}[/green]")

    if json_output or output:
        data = [tweet_to_dict(t) for t in tweets]
        if output:
            save_to_file(json.dumps(data, indent=2), output, 'json')
        if json_output and not output:
            console.print_json(json.dumps(data, indent=2))
    else:
        for tweet in tweets:
            display_tweet(tweet)
        console.print("=" * 60)


@x.group(name='accounts', invoke_without_command=True)
@click.pass_context
def x_accounts(ctx):
    """Manage X/Twitter accounts for authentication.

    \b
    Examples:
        fcrawl x accounts              # List all accounts
        fcrawl x accounts add file.txt # Add accounts from file
        fcrawl x accounts reset        # Reset rate limit locks
    """
    if ctx.invoked_subcommand is None:
        # Default behavior: list accounts
        _list_accounts()


def _list_accounts():
    """List all configured accounts."""
    async def _fetch():
        pool = get_x_pool()
        return await pool.accounts_info()

    try:
        accounts = asyncio.run(_fetch())
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()

    if not accounts:
        console.print("[yellow]No accounts configured.[/yellow]")
        console.print("\nTo add accounts, create a file with format:")
        console.print("  username:password:email:email_password")
        console.print("\nThen run: [bold]fcrawl x accounts add <file>[/bold]")
        return

    table = Table(title="X/Twitter Accounts")
    table.add_column("Username", style="cyan")
    table.add_column("Active", style="green")
    table.add_column("Logged In")
    table.add_column("Requests", justify="right")
    table.add_column("Last Used")
    table.add_column("Error", style="red")

    for acc in accounts:
        active = "[green]Yes[/green]" if acc["active"] else "[red]No[/red]"
        logged_in = "[green]Yes[/green]" if acc["logged_in"] else "[dim]No[/dim]"
        last_used = acc["last_used"].strftime("%Y-%m-%d %H:%M") if acc["last_used"] else "-"
        error = acc["error_msg"] or "-"

        table.add_row(
            acc["username"],
            active,
            logged_in,
            str(acc["total_req"]),
            last_used,
            error[:40] + "..." if len(error) > 40 else error,
        )

    console.print(table)
    console.print(f"\n[dim]Database: {get_x_db_path()}[/dim]")


@x_accounts.command(name='add')
@click.argument('file', type=click.Path(exists=True))
@click.option('--format', '-f', 'line_format', default='username:password:email:email_password',
              help='Line format (default: username:password:email:email_password)')
def x_accounts_add(file: str, line_format: str):
    """Add accounts from a file.

    \b
    File format (default): username:password:email:email_password
    One account per line.

    \b
    Examples:
        fcrawl x accounts add accounts.txt
        fcrawl x accounts add accounts.txt --format "username,password,email,email_password"
    """
    async def _add():
        pool = get_x_pool()
        await pool.load_from_file(file, line_format)
        # Try to login
        return await pool.login_all()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task("Adding and logging in accounts...", total=None)

        try:
            result = asyncio.run(_add())
            progress.stop()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    console.print(f"[green]Added {result['total']} accounts[/green]")
    console.print(f"  Success: {result['success']}")
    console.print(f"  Failed: {result['failed']}")

    if result['failed'] > 0:
        console.print("\n[yellow]Some accounts failed to login. Run 'fcrawl x accounts' to see details.[/yellow]")


@x_accounts.command(name='reset')
def x_accounts_reset():
    """Reset all rate limit locks on accounts.

    This unlocks accounts that are temporarily locked due to rate limits.
    """
    async def _reset():
        pool = get_x_pool()
        await pool.reset_locks()

    try:
        asyncio.run(_reset())
        console.print("[green]All account locks have been reset.[/green]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()


@x_accounts.command(name='login')
@click.argument('username', required=False)
def x_accounts_login(username: Optional[str]):
    """Login accounts (all inactive or specific username).

    \b
    Examples:
        fcrawl x accounts login           # Login all inactive accounts
        fcrawl x accounts login myuser    # Login specific account
    """
    async def _login():
        pool = get_x_pool()
        if username:
            return await pool.login_all([username])
        else:
            return await pool.login_all()

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task("Logging in accounts...", total=None)

        try:
            result = asyncio.run(_login())
            progress.stop()
        except Exception as e:
            progress.stop()
            console.print(f"[red]Error: {e}[/red]")
            raise click.Abort()

    console.print(f"[green]Login complete[/green]")
    console.print(f"  Total: {result['total']}")
    console.print(f"  Success: {result['success']}")
    console.print(f"  Failed: {result['failed']}")


@x_accounts.command(name='add-tokens')
@click.option('--ct0', required=True, help='ct0 cookie value from browser')
@click.option('--auth', 'auth_token', required=True, help='auth_token cookie value from browser')
@click.option('--name', 'username', default=None, help='Account name (auto-generated if not provided)')
def x_accounts_add_tokens(ct0: str, auth_token: str, username: Optional[str]):
    """Add account using browser tokens (ct0 + auth_token).

    \b
    Extract ct0 and auth_token cookies from your browser after logging into X/Twitter,
    then use this command to add the account without needing username/password.

    \b
    Examples:
        fcrawl x accounts add-tokens --ct0 "abc123..." --auth "xyz789..."
        fcrawl x accounts add-tokens --ct0 "abc123..." --auth "xyz789..." --name myaccount
    """
    import hashlib

    # Auto-generate username if not provided
    if not username:
        hash_input = f"{ct0[:16]}{auth_token[:16]}"
        short_hash = hashlib.md5(hash_input.encode()).hexdigest()[:8]
        username = f"token_{short_hash}"

    async def _add():
        pool = get_x_pool()
        return await pool.add_account_from_tokens(username, ct0, auth_token)

    try:
        success = asyncio.run(_add())
        if success:
            console.print(f"[green]Account '{username}' added successfully[/green]")
            console.print("  Status: active, logged_in")
        else:
            console.print(f"[yellow]Account '{username}' already exists[/yellow]")
    except Exception as e:
        console.print(f"[red]Error: {e}[/red]")
        raise click.Abort()
