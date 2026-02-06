"""Parse X/Twitter article content from Draft.js format to markdown."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass
class Article:
    """Represents an X/Twitter article."""
    rest_id: str
    title: str
    preview_text: str
    content_blocks: list[dict]
    cover_image_url: str | None
    author_username: str | None
    author_name: str | None
    created_at: datetime | None

    def to_markdown(self) -> str:
        """Convert article to markdown format."""
        return draftjs_to_markdown(self.content_blocks, self.title)

    def to_dict(self) -> dict:
        """Convert article to dictionary for JSON output."""
        return {
            "rest_id": self.rest_id,
            "title": self.title,
            "preview_text": self.preview_text,
            "cover_image_url": self.cover_image_url,
            "author_username": self.author_username,
            "author_name": self.author_name,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "content_blocks": self.content_blocks,
            "markdown": self.to_markdown(),
        }


def apply_inline_styles(text: str, style_ranges: list[dict]) -> str:
    """Apply inline styles (bold, italic) to text."""
    if not style_ranges:
        return text

    # Sort by offset descending so we can apply from end to start
    # This prevents offset shifting issues
    sorted_ranges = sorted(style_ranges, key=lambda x: x.get("offset", 0), reverse=True)

    result = text
    for style in sorted_ranges:
        offset = style.get("offset", 0)
        length = style.get("length", 0)
        style_type = style.get("style", "")

        if offset < 0 or offset + length > len(result):
            continue

        styled_text = result[offset:offset + length]

        if style_type == "Bold":
            styled_text = f"**{styled_text}**"
        elif style_type == "Italic":
            styled_text = f"*{styled_text}*"
        elif style_type == "Code":
            styled_text = f"`{styled_text}`"

        result = result[:offset] + styled_text + result[offset + length:]

    return result


def draftjs_to_markdown(blocks: list[dict], title: str | None = None) -> str:
    """Convert Draft.js blocks to markdown.

    Block types:
    - unstyled: paragraph
    - header-one: # heading
    - header-two: ## heading
    - header-three: ### heading
    - unordered-list-item: - item
    - ordered-list-item: 1. item
    - blockquote: > quote
    - code-block: ```code```

    Inline styles:
    - Bold: **text**
    - Italic: *text*
    """
    lines = []

    if title:
        lines.append(f"# {title}")
        lines.append("")

    list_counter = 0
    prev_type = None

    for block in blocks:
        block_type = block.get("type", "unstyled")
        text = block.get("text", "")
        style_ranges = block.get("inlineStyleRanges", [])

        # Apply inline styles
        styled_text = apply_inline_styles(text, style_ranges)

        # Reset list counter when leaving ordered list
        if prev_type == "ordered-list-item" and block_type != "ordered-list-item":
            list_counter = 0

        # Convert block type to markdown
        if block_type == "unstyled":
            if styled_text.strip():
                lines.append(styled_text)
                lines.append("")
        elif block_type == "header-one":
            lines.append(f"# {styled_text}")
            lines.append("")
        elif block_type == "header-two":
            lines.append(f"## {styled_text}")
            lines.append("")
        elif block_type == "header-three":
            lines.append(f"### {styled_text}")
            lines.append("")
        elif block_type == "unordered-list-item":
            lines.append(f"- {styled_text}")
        elif block_type == "ordered-list-item":
            list_counter += 1
            lines.append(f"{list_counter}. {styled_text}")
        elif block_type == "blockquote":
            lines.append(f"> {styled_text}")
            lines.append("")
        elif block_type == "code-block":
            lines.append(f"```")
            lines.append(styled_text)
            lines.append("```")
            lines.append("")
        else:
            # Unknown type, treat as paragraph
            if styled_text.strip():
                lines.append(styled_text)
                lines.append("")

        # Add blank line after list ends
        if prev_type in ("unordered-list-item", "ordered-list-item") and block_type not in ("unordered-list-item", "ordered-list-item"):
            lines.insert(-1, "")

        prev_type = block_type

    # Clean up multiple blank lines
    result = "\n".join(lines)
    while "\n\n\n" in result:
        result = result.replace("\n\n\n", "\n\n")

    return result.strip()


def parse_article_from_response(response_json: dict) -> Article | None:
    """Extract article from tweet API response.

    Looks for article at:
    - data.tweetResult.result.article.article_results.result (TweetResultByRestId)
    - data.threaded_conversation_with_injections_v2.instructions[].entries[].content.itemContent.tweet_results.result.article.article_results.result (TweetDetail)
    """
    article_data = None
    user_data = None

    # Try TweetResultByRestId format
    article_data = _get_nested(response_json, [
        "data", "tweetResult", "result", "article", "article_results", "result"
    ])
    if article_data:
        user_data = _get_nested(response_json, [
            "data", "tweetResult", "result", "core", "user_results", "result"
        ])

    # Try TweetDetail format
    if not article_data:
        instructions = _get_nested(response_json, [
            "data", "threaded_conversation_with_injections_v2", "instructions"
        ])
        if instructions:
            for instruction in instructions:
                if instruction.get("type") == "TimelineAddEntries":
                    entries = instruction.get("entries", [])
                    for entry in entries:
                        article_data = _get_nested(entry, [
                            "content", "itemContent", "tweet_results", "result",
                            "article", "article_results", "result"
                        ])
                        if article_data:
                            # Also get user info
                            user_data = _get_nested(entry, [
                                "content", "itemContent", "tweet_results", "result",
                                "core", "user_results", "result"
                            ])
                            break
                    if article_data:
                        break

    if not article_data:
        return None

    # Extract content_state blocks
    content_state = article_data.get("content_state", {})
    blocks = content_state.get("blocks", [])

    if not blocks:
        return None

    # Extract cover image
    cover_media = article_data.get("cover_media", {})
    media_info = cover_media.get("media_info", {})
    cover_image_url = media_info.get("original_img_url")

    # Extract author info
    author_username = None
    author_name = None
    if user_data:
        author_username = _get_nested(user_data, ["core", "screen_name"])
        author_name = _get_nested(user_data, ["core", "name"])

    # Extract created_at
    created_at = None
    metadata = article_data.get("metadata", {})
    first_published = metadata.get("first_published_at_secs")
    if first_published:
        created_at = datetime.fromtimestamp(first_published)

    return Article(
        rest_id=article_data.get("rest_id", ""),
        title=article_data.get("title", ""),
        preview_text=article_data.get("preview_text", ""),
        content_blocks=blocks,
        cover_image_url=cover_image_url,
        author_username=author_username,
        author_name=author_name,
        created_at=created_at,
    )


def _get_nested(obj: Any, keys: list[str]) -> Any:
    """Safely get nested dictionary value."""
    current = obj
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
        if current is None:
            return None
    return current
