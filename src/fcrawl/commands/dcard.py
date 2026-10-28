"""Dcard commands for fcrawl.

Dcard pages sit behind Cloudflare, but the JSON endpoints its own frontend
uses are reachable through Firecrawl (curl gets 403). Each request costs one
Firecrawl credit and goes through the key pool like `scrape`.
"""

import json
import re
from typing import Any, Optional

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.config import firecrawl_call
from ..utils.output import resolve_pretty, save_to_file

# Progress/errors on stderr so `--json | jq` and `> file` stay clean
console = Console(stderr=True)

API = "https://www.dcard.tw/service/api/v2"
POST_URL_RE = re.compile(r"dcard\.tw/f/[^/]+/p/(\d+)")
DEFAULT_TIMEOUT = 40  # Firecrawl has been seen taking ~20s on these endpoints


def parse_post_id(url_or_id: str) -> str:
    if url_or_id.isdigit():
        return url_or_id
    match = POST_URL_RE.search(url_or_id)
    if match:
        return match.group(1)
    raise click.BadParameter(f"Cannot parse Dcard post URL/ID: {url_or_id}")


def fetch_json(url: str, timeout: int, no_cache: bool) -> Any:
    """Scrape a Dcard API URL via Firecrawl and parse the JSON it returns."""
    key = cache_key(url)
    if not no_cache:
        cached = read_cache("dcard", key)
        if cached is not None:
            return cached
    result = firecrawl_call(
        lambda client: client.scrape(url, formats=["markdown"], timeout=timeout * 1000)
    )
    status = getattr(result.metadata, "status_code", None)
    if isinstance(status, int) and status >= 400:
        raise RuntimeError(f"Dcard API returned {status} for {url}")
    # Firecrawl wraps the JSON body in a ```json fence; strip only the outer
    # fence lines so backticks inside post content cannot cut the JSON short.
    markdown = (result.markdown or "").strip()
    if markdown.startswith("```"):
        markdown = markdown.split("\n", 1)[1] if "\n" in markdown else ""
        markdown = markdown.rsplit("```", 1)[0]
    data = json.loads(markdown)
    if isinstance(data, dict) and "error" in data and "id" not in data:
        raise RuntimeError(f"Dcard API error: {data.get('message') or data['error']}")
    write_cache("dcard", key, data)
    return data


def fetch_comments(post_id: str, limit: int, timeout: int, no_cache: bool) -> list:
    """Page through comments (30 per request, `after` = last floor seen)."""
    comments: list = []
    after = 0
    while limit == 0 or len(comments) < limit:
        url = f"{API}/posts/{post_id}/comments?limit=30"
        if after:
            url += f"&after={after}"
        page = fetch_json(url, timeout, no_cache)
        if not page:
            break
        comments.extend(page)
        after = page[-1].get("floor") or 0
        if len(page) < 30:
            break
    return comments[:limit] if limit else comments


def post_to_dict(post: dict) -> dict:
    return {
        "id": post.get("id"),
        "title": post.get("title"),
        "forum": post.get("forumName"),
        "forum_alias": post.get("forumAlias"),
        "author": post.get("school"),
        "author_detail": post.get("department"),
        "gender": post.get("gender"),
        "created_at": post.get("createdAt"),
        "updated_at": post.get("updatedAt"),
        "likes": post.get("likeCount"),
        "comment_count": post.get("commentCount"),
        "topics": post.get("topics") or [],
        "content": post.get("content"),
        "url": f"https://www.dcard.tw/f/{post.get('forumAlias')}/p/{post.get('id')}",
    }


def comment_to_dict(comment: dict) -> dict:
    return {
        "floor": comment.get("floor"),
        "author": comment.get("school"),
        "author_detail": comment.get("department"),
        "gender": comment.get("gender"),
        "is_op": bool(comment.get("host")),
        "created_at": comment.get("createdAt"),
        "likes": comment.get("likeCount"),
        "replies": comment.get("subCommentCount"),
        "hidden": bool(comment.get("hidden")),
        "content": comment.get("content"),
    }


def render_markdown(post: dict, comments: list) -> str:
    p = post_to_dict(post)
    author = p["author"] or "匿名"
    if p["author_detail"]:
        author += f" ({p['author_detail']})"
    lines = [
        f"# {p['title']}",
        "",
        f"- 看板: {p['forum']}  作者: {author}  性別: {p['gender']}",
        f"- 發文: {p['created_at']}  讚: {p['likes']}  留言: {p['comment_count']}",
        f"- 話題: {', '.join(p['topics'])}" if p["topics"] else None,
        f"- URL: {p['url']}",
        "",
        p["content"] or "",
    ]
    if comments:
        lines += ["", "---", "", f"## 留言 ({len(comments)})", ""]
        for raw in comments:
            c = comment_to_dict(raw)
            who = c["author"] or "匿名"
            if c["is_op"]:
                who += " (原PO)"
            head = f"**B{c['floor']}** {who} · 讚 {c['likes']}"
            if c["replies"]:
                head += f" · 回覆 {c['replies']}"
            body = "(已隱藏)" if c["hidden"] else (c["content"] or "")
            lines += [head, "", body, ""]
    return "\n".join(line for line in lines if line is not None)


@click.group()
def dcard():
    """Read Dcard posts via Dcard's own JSON API (through Firecrawl)"""


@dcard.command(name="post")
@click.argument("url_or_id")
@click.option(
    "--limit", "-l", type=int, default=30, show_default=True,
    help="Max comments to fetch (0 = all, 30 per credit)",
)
@click.option("--no-comments", is_flag=True, help="Fetch only the post")
@click.option("-o", "--output", help="Save output to file")
@click.option("--json", "json_output", is_flag=True, help="Output as JSON")
@click.option("--pretty/--no-pretty", default=None, help="Pretty print output")
@click.option(
    "--timeout", type=int, default=DEFAULT_TIMEOUT, show_default=True,
    help="Seconds to give Firecrawl per request",
)
@click.option("--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch")
def dcard_post(
    url_or_id: str,
    limit: int,
    no_comments: bool,
    output: Optional[str],
    json_output: bool,
    pretty: Optional[bool],
    timeout: int,
    no_cache: bool,
):
    """Fetch a Dcard post with its comments.

    \b
    Examples:
        fcrawl dcard post https://www.dcard.tw/f/military/p/261218190
        fcrawl dcard post 261218190 --limit 0        # all comments
        fcrawl dcard post 261218190 --no-comments --json
    """
    pretty = resolve_pretty(pretty)
    post_id = parse_post_id(url_or_id)

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
    ) as progress:
        progress.add_task(f"Fetching Dcard post {post_id}...", total=None)
        try:
            post = fetch_json(f"{API}/posts/{post_id}", timeout, no_cache)
            comments = [] if no_comments else fetch_comments(post_id, limit, timeout, no_cache)
        except Exception as exc:
            progress.stop()
            console.print(f"[red]Error: {exc}[/red]")
            raise click.Abort()

    if json_output:
        text = json.dumps(
            {"post": post_to_dict(post), "comments": [comment_to_dict(c) for c in comments]},
            ensure_ascii=False,
            indent=2 if pretty else None,
        )
        if output:
            save_to_file(text, output, "json")
        else:
            print(text)
        return

    text = render_markdown(post, comments)
    if output:
        save_to_file(text, output, "markdown")
    else:
        print(text)
