"""Dcard commands for fcrawl.

Dcard pages sit behind Cloudflare, but the JSON endpoints its own frontend
uses are reachable through Firecrawl (curl gets 403). Each request costs one
Firecrawl credit and goes through the key pool like `scrape`. Endpoint
details: ~/30-39-Research/34-tech-research/2026-10-29-dcard-api.md
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn

from ..utils.cache import cache_key, read_cache, write_cache
from ..utils.config import firecrawl_call
from ..utils.output import resolve_pretty, save_to_file

# Progress/errors on stderr so `--json | jq` and `> file` stay clean
console = Console(stderr=True)

API = "https://www.dcard.tw/service/api/v2"
API3 = "https://www.dcard.tw/service/api/v3"
POST_URL_RE = re.compile(r"dcard\.tw/f/[^/]+/p/(\d+)")
DEFAULT_TIMEOUT = 40  # Firecrawl has been seen taking ~20s on these endpoints
SEARCH_PAGE = 30  # v3 search caps limit at 30
FORUM_PAGE = 100  # v2 forum list returns up to 100 per request
COMMENT_PAGE = 30
TAIPEI = timezone(timedelta(hours=8))

# Per-process request accounting, printed on stderr at the end of a command
stats = {"requests": 0, "cached": 0}


def tw_time(iso: Optional[str]) -> str:
    """'2026-04-01T10:30:04.028Z' -> '2026-04-01 18:30' (Taiwan time)."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:16]
    return dt.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M")


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
        if cached is not None and not is_error_envelope(cached):
            stats["cached"] += 1
            return cached
    result = None
    for attempt in (1, 2):
        stats["requests"] += 1
        try:
            result = firecrawl_call(
                lambda client: client.scrape(url, formats=["markdown"], timeout=timeout * 1000)
            )
            break
        except Exception as exc:
            # Firecrawl occasionally stalls on these endpoints; one retry
            # (which the pool may route to another key) almost always works.
            if attempt == 1 and "timed out" in str(exc).lower():
                console.print(f"[yellow]Firecrawl timeout ({timeout}s), retrying once…[/yellow]")
                continue
            if "timed out" in str(exc).lower():
                raise RuntimeError(f"Firecrawl timed out twice ({timeout}s) on {url} — try --timeout 60")
            raise
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
    if is_error_envelope(data):
        raise RuntimeError(f"Dcard API error: {data.get('message') or data['error']}")
    write_cache("dcard", key, data)
    return data


def is_error_envelope(data: Any) -> bool:
    return isinstance(data, dict) and "error" in data and "id" not in data


# --- comments (v3) ---------------------------------------------------------


def fetch_comments(
    post_id: str, limit: int, sort: str, timeout: int, no_cache: bool, parent_id: Optional[str] = None
) -> list:
    """Page through v3 comments; cursor = last doorplate (or its second part for replies)."""
    comments: list = []
    cursor = None
    while limit == 0 or len(comments) < limit:
        params = {"sort": sort, "limit": COMMENT_PAGE}
        if parent_id:
            params["parentId"] = parent_id
        if cursor:
            params["cursor"] = cursor
        page = fetch_json(f"{API3}/posts/{post_id}/comments?{urlencode(params)}", timeout, no_cache)
        items = page.get("items") if isinstance(page, dict) else page
        if not items:
            break
        comments.extend(items)
        doorplate = str(items[-1].get("doorplate") or items[-1].get("floor") or "")
        cursor = doorplate.split("-")[-1]
        if len(items) < COMMENT_PAGE:
            break
    return comments[:limit] if limit else comments


def attach_replies(
    post_id: str, comments: list, max_threads: int, sort: str, timeout: int, no_cache: bool,
    by_likes: bool = False,
) -> None:
    """Fetch sub-replies for up to max_threads threads (1 credit per 30 replies each).

    by_likes picks the most-liked threads instead of the first ones in order.
    """
    threads = [c for c in comments if c.get("subCommentCount") and c.get("id")]
    if by_likes:
        threads.sort(key=lambda c: c.get("likeCount") or 0, reverse=True)
    for c in threads[:max_threads]:
        c["_replies"] = fetch_comments(post_id, 0, sort, timeout, no_cache, parent_id=c["id"])


# --- post lists (search / forum) --------------------------------------------


def fetch_search(
    query: str, limit: int, sort: str, field: str, forum: Optional[str], since_days: Optional[int],
    timeout: int, no_cache: bool,
) -> tuple[list, list]:
    """v3 search. Next page = the same params plus nextKey. Returns (posts, related queries)."""
    params = {
        "query": query, "field": field, "sort": sort, "limit": SEARCH_PAGE,
        "highlight": "false", "country": "TW", "nsfw": "false", "platform": "web",
    }
    if forum:
        params["forum"] = forum
    if since_days:
        params["since"] = int(time.time()) - since_days * 86400
    posts: list = []
    related: list = []
    next_key = None
    # nextKey is an offset, not a snapshot: mixing a cached page with a live
    # later page can repeat or skip posts, so multi-page searches skip cache.
    multi_page = limit == 0 or limit > SEARCH_PAGE
    while limit == 0 or len(posts) < limit:
        page_params = dict(params, nextKey=next_key) if next_key else params
        page = fetch_json(
            f"{API3}/search/posts?{urlencode(page_params)}", timeout, no_cache or multi_page
        )
        found = 0
        for item in page.get("items") or []:
            if "searchPost" in item:
                posts.append(item["searchPost"]["post"])
                found += 1
            elif "searchQueryRecommendationList" in item and not related:
                related = [
                    r.get("name") for r in item["searchQueryRecommendationList"].get("searchQueryRecommendations") or []
                ]
        next_key = page.get("nextKey")
        if not found or not next_key:
            break
    return (posts[:limit] if limit else posts), related


def fetch_forum_posts(alias: str, limit: int, popular: bool, timeout: int, no_cache: bool) -> list:
    """v2 forum list, 100 per credit; before=<last id> pages backwards."""
    posts: list = []
    before = None
    while limit == 0 or len(posts) < limit:
        per_page = FORUM_PAGE if limit == 0 else min(FORUM_PAGE, limit - len(posts))
        params = {"limit": per_page, "popular": "true" if popular else "false"}
        if before:
            params["before"] = before
        page = fetch_json(f"{API}/forums/{alias}/posts?{urlencode(params)}", timeout, no_cache)
        if not page:
            break
        posts.extend(page)
        before = page[-1].get("id")
        if len(page) < per_page or popular:  # popular is one short list
            break
    return posts[:limit] if limit else posts


# --- shaping ---------------------------------------------------------------


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
        "total_comment_count": post.get("totalCommentCount"),
        "topics": post.get("topics") or [],
        "excerpt": post.get("excerpt"),
        "content": post.get("content"),
        "url": f"https://www.dcard.tw/f/{post.get('forumAlias')}/p/{post.get('id')}",
    }


def comment_to_dict(comment: dict) -> dict:
    author = comment.get("author") or {}
    d = {
        "floor": comment.get("floor"),
        "doorplate": comment.get("doorplate"),
        "author": comment.get("school"),
        "author_detail": comment.get("department"),
        "gender": comment.get("gender"),
        "is_op": bool(comment.get("host") or author.get("isPostAuthor")),
        "created_at": comment.get("createdAt"),
        "likes": comment.get("likeCount"),
        "replies": comment.get("subCommentCount"),
        "hidden": bool(comment.get("hidden")),
        "content": comment.get("content"),
    }
    if "_replies" in comment:
        d["reply_list"] = [comment_to_dict(r) for r in comment["_replies"]]
    return d


def forum_to_dict(forum: dict) -> dict:
    return {
        "alias": forum.get("alias"),
        "name": forum.get("name"),
        "subscribers": forum.get("subscriptionCount"),
        "posts_30d": (forum.get("postCount") or {}).get("last30Days"),
        "nsfw": bool(forum.get("nsfw")),
        "school": bool(forum.get("isSchool")),
        "description": forum.get("description"),
    }


def render_post(post: dict, comments: list) -> str:
    p = post_to_dict(post)
    author = p["author"] or "匿名"
    if p["author_detail"]:
        author += f" ({p['author_detail']})"
    lines = [
        f"# {p['title']}",
        "",
        f"- 看板: {p['forum']}  作者: {author}  性別: {p['gender']}",
        f"- 發文: {tw_time(p['created_at'])}  讚: {p['likes']}  留言: {p['comment_count']}"
        + (f" (含回覆 {p['total_comment_count']})" if p["total_comment_count"] and p["total_comment_count"] != p["comment_count"] else ""),
        f"- 話題: {', '.join(p['topics'])}" if p["topics"] else None,
        f"- URL: {p['url']}",
        "",
        p["content"] or "",
    ]
    if comments:
        total = p["comment_count"] or len(comments)
        shown = f"顯示 {len(comments)} / 共 {total}" if len(comments) < total else f"共 {len(comments)}"
        if len(comments) < total:
            shown += "，--limit 0 看全部"
        lines += ["", "---", "", f"## 留言（{shown}）", ""]
        for raw in comments:
            lines += render_comment(raw)
    return "\n".join(line for line in lines if line is not None)


def render_comment(raw: dict, indent: str = "") -> list:
    c = comment_to_dict(raw)
    who = c["author"] or "匿名"
    if c["is_op"]:
        who += " (原PO)"
    label = c["doorplate"] or c["floor"]
    if c["hidden"]:
        head = f"{indent}**B{label}** (已隱藏)"
        body = ""
    else:
        head = f"{indent}**B{label}** {who} · 讚 {c['likes'] or 0} · {tw_time(c['created_at'])}"
        body = c["content"] or ""
    if c["replies"]:
        head += f" · 回覆 {c['replies']}"
    if indent:
        body = "\n".join(indent + line for line in body.splitlines()) or indent
    lines = [head, "", body, ""]
    for reply in raw.get("_replies") or []:
        lines += render_comment(reply, indent + "> ")
    return lines


def filter_posts(posts: list, min_likes: int, by_likes: bool) -> list:
    if min_likes:
        posts = [p for p in posts if (p.get("likeCount") or 0) >= min_likes]
    if by_likes:
        posts = sorted(posts, key=lambda p: p.get("likeCount") or 0, reverse=True)
    return posts


def render_post_list(
    title: str, posts: list, related: Optional[list] = None, compact: bool = False
) -> str:
    lines = [f"# {title}", "", f"{len(posts)} 篇", ""]
    if compact:
        lines.append("| 讚 | 留言 | 日期 | ID | 標題 |")
        lines.append("|---:|---:|---|---|---|")
    for raw in posts:
        p = post_to_dict(raw)
        date = tw_time(p["created_at"])[:10]
        if compact:
            title_cell = (p["title"] or "").replace("|", "\\|")
            lines.append(f"| {p['likes']} | {p['comment_count']} | {date} | {p['id']} | {title_cell} |")
            continue
        comments = f"留言 {p['comment_count']}"
        if p["total_comment_count"] and p["total_comment_count"] != p["comment_count"]:
            comments += f" (含回覆 {p['total_comment_count']})"
        author = p["author"] or "匿名"
        topics = f" #{' #'.join(p['topics'][:4])}" if p["topics"] else ""
        lines.append(f"- **{p['title']}**  [{p['forum']}] {author} · 讚 {p['likes']} {comments} · {date}{topics}")
        lines.append(f"  {p['url']}")
        if p["excerpt"]:
            lines.append(f"  {p['excerpt'][:160]}")
    if related:
        lines += ["", f"相關搜尋: {', '.join(r for r in related if r)}"]
    return "\n".join(lines)


def emit(data_for_json, text: str, output: Optional[str], json_output: bool, pretty: bool) -> None:
    if json_output:
        text = json.dumps(data_for_json, ensure_ascii=False, indent=2 if pretty else None)
        fmt = "json"
    else:
        fmt = "markdown"
    if output:
        save_to_file(text, output, fmt)
    else:
        print(text)
    console.print(f"[dim]credits: {stats['requests']} ({stats['cached']} cached)[/dim]")


def run(label: str, fn):
    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"), console=console
    ) as progress:
        progress.add_task(label, total=None)
        try:
            return fn()
        except Exception as exc:
            progress.stop()
            console.print(f"[red]Error: {exc}[/red]")
            raise click.Abort()


def list_options(fn):
    for opt in reversed([
        click.option("--compact", is_flag=True, help="One table row per post, no excerpt (fits 300 posts in ~10k tokens)"),
        click.option("--min-likes", type=int, default=0, help="Drop posts with fewer likes (after fetching)"),
        click.option("--by-likes", is_flag=True, help="Sort the fetched posts by likes"),
    ]):
        fn = opt(fn)
    return fn


def common_options(fn):
    for opt in reversed([
        click.option("-o", "--output", help="Save output to file"),
        click.option("--json", "json_output", is_flag=True, help="Output as JSON"),
        click.option("--pretty/--no-pretty", default=None, help="Pretty print output"),
        click.option(
            "--timeout", type=int, default=DEFAULT_TIMEOUT, show_default=True,
            help="Seconds to give Firecrawl per request",
        ),
        click.option("--no-cache", "no_cache", is_flag=True, help="Bypass cache, force fresh fetch"),
    ]):
        fn = opt(fn)
    return fn


# --- commands --------------------------------------------------------------


@click.group()
def dcard():
    """Read Dcard via Dcard's own JSON API (through Firecrawl, 1 credit per request)"""


@dcard.command(name="post")
@click.argument("url_or_ids", nargs=-1, required=True)
@click.option(
    "--limit", "-l", type=int, default=30, show_default=True,
    help="Max top-level comments (0 = all, 30 per credit)",
)
@click.option(
    "--sort", type=click.Choice(["oldest", "newest", "likes"]), default="oldest", show_default=True,
    help="Comment order",
)
@click.option(
    "--replies", default="0", show_default=True,
    help="Expand sub-replies: N = first N threads in order, 'auto' = 3 most-liked threads, "
         "'auto:N' = N most-liked (1 credit per 30 replies in each thread)",
)
@click.option("--no-comments", is_flag=True, help="Fetch only the post")
@click.option("--parallel", type=int, default=3, show_default=True, help="Concurrent fetches when given several posts")
@common_options
def dcard_post(url_or_ids, limit, sort, replies, no_comments, parallel, output, json_output, pretty, timeout, no_cache):
    """Fetch one or more Dcard posts with comments.

    With several IDs, -o must be a directory (one <id>.md / <id>.json per post).

    \b
    Examples:
        fcrawl dcard post https://www.dcard.tw/f/military/p/261218190
        fcrawl dcard post 261218190 --limit 0 --replies 10   # all comments, expand 10 reply threads
        fcrawl dcard post 261218190 --sort likes --replies auto
        fcrawl dcard post 261218190 258383705 258164563 -o posts/   # 3 at a time
    """
    pretty = resolve_pretty(pretty)
    ids = [parse_post_id(x) for x in url_or_ids]
    if replies == "auto":
        reply_threads, reply_by_likes = 3, True
    elif replies.startswith("auto:"):
        reply_threads, reply_by_likes = int(replies.split(":", 1)[1]), True
    else:
        reply_threads, reply_by_likes = int(replies), False

    def fetch_one(post_id: str):
        post = fetch_json(f"{API}/posts/{post_id}", timeout, no_cache)
        comments = [] if no_comments else fetch_comments(post_id, limit, sort, timeout, no_cache)
        if reply_threads:
            attach_replies(post_id, comments, reply_threads, "oldest", timeout, no_cache, by_likes=reply_by_likes)
        return post, comments

    if len(ids) == 1:
        post, comments = run(f"Fetching Dcard post {ids[0]}...", lambda: fetch_one(ids[0]))
        emit(
            {"post": post_to_dict(post), "comments": [comment_to_dict(c) for c in comments]},
            render_post(post, comments), output, json_output, pretty,
        )
        return

    if not output:
        raise click.BadParameter("give -o <directory> when fetching several posts", param_hint="-o")
    out_dir = Path(output)
    out_dir.mkdir(parents=True, exist_ok=True)

    def work():
        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            return list(pool.map(fetch_one, ids))

    results = run(f"Fetching {len(ids)} Dcard posts ({parallel} at a time)...", work)
    for post_id, (post, comments) in zip(ids, results):
        path = out_dir / f"{post_id}.{'json' if json_output else 'md'}"
        if json_output:
            path.write_text(json.dumps(
                {"post": post_to_dict(post), "comments": [comment_to_dict(c) for c in comments]},
                ensure_ascii=False, indent=2 if pretty else None,
            ))
        else:
            path.write_text(render_post(post, comments))
        p = post_to_dict(post)
        print(f"RESULT path={path} id={post_id} likes={p['likes']} comments={p['comment_count']} title={p['title']}")
    console.print(f"[dim]credits: {stats['requests']} ({stats['cached']} cached)[/dim]")


@dcard.command(name="search")
@click.argument("query")
@click.option("--forum", "-f", help="Restrict to a forum alias (e.g. military)")
@click.option(
    "--sort", type=click.Choice(["relevance", "latest", "trending", "interaction"]),
    default="relevance", show_default=True,
)
@click.option(
    "--field", type=click.Choice(["all", "title", "topics"]), default="all", show_default=True,
    help="Where to match the query",
)
@click.option("--since", type=int, help="Only posts from the last N days")
@click.option(
    "--limit", "-l", type=int, default=30, show_default=True, help="Max posts (0 = all, 30 per credit)"
)
@list_options
@common_options
def dcard_search(query, forum, sort, field, since, limit, compact, min_likes, by_likes, output, json_output, pretty, timeout, no_cache):
    """Search Dcard posts (titles + excerpts; use `dcard post` for full text).

    \b
    Examples:
        fcrawl dcard search "當兵 新訓" --forum military --sort latest
        fcrawl dcard search 成功嶺 --since 30 --limit 60 --json
    """
    pretty = resolve_pretty(pretty)
    posts, related = run(
        f"Searching Dcard for {query}...",
        lambda: fetch_search(query, limit, sort, field, forum, since, timeout, no_cache),
    )
    posts = filter_posts(posts, min_likes, by_likes)
    title = f"Dcard 搜尋「{query}」" + (f" @ {forum}" if forum else "") + f" · {sort}"
    emit(
        {"query": query, "posts": [post_to_dict(p) for p in posts], "related_queries": related},
        render_post_list(title, posts, related, compact), output, json_output, pretty,
    )


@dcard.command(name="topic")
@click.argument("name")
@click.option("--hot", is_flag=True, help="Trending instead of latest")
@click.option("--forum", "-f", help="Restrict to a forum alias")
@click.option(
    "--limit", "-l", type=int, default=30, show_default=True, help="Max posts (0 = all, 30 per credit)"
)
@list_options
@common_options
def dcard_topic(name, hot, forum, limit, compact, min_likes, by_likes, output, json_output, pretty, timeout, no_cache):
    """Posts tagged with a topic (Dcard's topic page = search with field=topics).

    \b
    Examples:
        fcrawl dcard topic 當兵
        fcrawl dcard topic 新訓 --hot --limit 60
    """
    pretty = resolve_pretty(pretty)
    sort = "trending" if hot else "latest"
    posts, _ = run(
        f"Fetching Dcard topic {name}...",
        lambda: fetch_search(name, limit, sort, "topics", forum, None, timeout, no_cache),
    )
    posts = filter_posts(posts, min_likes, by_likes)
    emit(
        {"topic": name, "posts": [post_to_dict(p) for p in posts]},
        render_post_list(f"Dcard 話題「{name}」 · {sort}", posts, compact=compact), output, json_output, pretty,
    )


@dcard.command(name="forum")
@click.argument("alias")
@click.option("--popular", is_flag=True, help="Popular list instead of latest")
@click.option(
    "--limit", "-l", type=int, default=100, show_default=True, help="Max posts (0 = all, 100 per credit)"
)
@click.option("--info", is_flag=True, help="Also fetch forum info (1 extra credit)")
@list_options
@common_options
def dcard_forum(alias, popular, limit, info, compact, min_likes, by_likes, output, json_output, pretty, timeout, no_cache):
    """List posts in a forum (latest by default).

    \b
    Examples:
        fcrawl dcard forum military
        fcrawl dcard forum military --popular
        fcrawl dcard forum military --limit 300 --compact --min-likes 10
    """
    pretty = resolve_pretty(pretty)

    def work():
        meta = fetch_json(f"{API}/forums/{alias}", timeout, no_cache) if info else None
        return meta, fetch_forum_posts(alias, limit, popular, timeout, no_cache)

    meta, posts = run(f"Fetching Dcard forum {alias}...", work)
    posts = filter_posts(posts, min_likes, by_likes)
    title = f"Dcard 看板 {alias} · {'熱門' if popular else '最新'}"
    text = render_post_list(title, posts, compact=compact)
    if meta:
        f = forum_to_dict(meta)
        text = f"{f['name']}（{alias}）· 訂閱 {f['subscribers']} · 近 30 天 {f['posts_30d']} 篇\n{f['description'] or ''}\n\n" + text
    emit(
        {"forum": forum_to_dict(meta) if meta else alias, "posts": [post_to_dict(p) for p in posts]},
        text, output, json_output, pretty,
    )


@dcard.command(name="forums")
@click.option("--school", is_flag=True, help="Include school forums")
@click.option("--search", "-s", "needle", help="Filter by alias/name substring")
@click.option("--limit", "-l", type=int, default=50, show_default=True, help="Max forums to show (0 = all)")
@common_options
def dcard_forums(school, needle, limit, output, json_output, pretty, timeout, no_cache):
    """List Dcard forums, biggest first (one 1-credit request, cached)."""
    pretty = resolve_pretty(pretty)
    forums = run("Fetching Dcard forum list...", lambda: fetch_json(f"{API}/forums", timeout, no_cache))
    rows = [forum_to_dict(f) for f in forums if not f.get("invisible")]
    if not school:
        rows = [r for r in rows if not r["school"]]
    if needle:
        n = needle.lower()
        rows = [r for r in rows if n in (r["alias"] or "").lower() or n in (r["name"] or "").lower()]
    rows.sort(key=lambda r: r["subscribers"] or 0, reverse=True)
    if limit:
        rows = rows[:limit]
    lines = [f"# Dcard 看板 ({len(rows)})", ""]
    for r in rows:
        lines.append(f"- `{r['alias']}`  {r['name']}  訂閱 {r['subscribers']} · 近 30 天 {r['posts_30d']} 篇")
    emit({"forums": rows}, "\n".join(lines), output, json_output, pretty)
