"""Map twitterapi.io JSON responses to twscrape's Tweet / User dataclasses.

The goal of this module is to make ``commands/x.py`` backend-agnostic:
whichever backend produces the data, ``display_tweet()`` and
``tweet_to_dict()`` receive the same dataclass shape.

twitterapi.io's schema is documented at:
    https://docs.twitterapi.io/api-reference/endpoint/get_tweet_by_ids.md

The mapper is tolerant of missing fields — the OpenAPI marks most
properties optional — so we use ``.get()`` with sensible defaults
everywhere and avoid KeyErrors on partial responses.

Known gap: the OpenAPI schema does NOT document any photo/video
media URL fields on the Tweet. ``to_media()`` currently returns an
empty ``Media`` instance with a TODO. Verify this against live
responses once an API key is configured.
"""

from __future__ import annotations

import email.utils
from datetime import datetime, timezone
from typing import Any

from ..twscrape.models import (
    Media,
    TextLink,
    Tweet,
    User,
    UserRef,
)


# ---- helpers ---------------------------------------------------------------

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _parse_date(value: Any) -> datetime:
    """Parse twitterapi.io's RFC-2822-ish createdAt. Returns epoch on failure."""
    if not value:
        return _EPOCH
    try:
        dt = email.utils.parsedate_to_datetime(value)
        # Normalize naive → UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (TypeError, ValueError):
        return _EPOCH


def _int(value: Any, default: int = 0) -> int:
    """Coerce to int, returning default on None or parse failure."""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _int_opt(value: Any) -> int | None:
    """Optional-int coercion: None if missing, int otherwise."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# ---- user mapper -----------------------------------------------------------

def to_user(obj: dict) -> User:
    """Convert a twitterapi.io ``UserInfo`` dict to a twscrape ``User``.

    Fields not present in twitterapi.io's schema default to safe values:
      - ``listedCount``: 0 (not exposed)
      - ``verified``: falls back to ``isBlueVerified``
      - ``descriptionLinks``: parsed from ``profile_bio.entities`` if present
    """
    screen_name = obj.get("userName") or ""
    user_id_str = str(obj.get("id") or "0")
    profile_url = obj.get("url") or f"https://x.com/{screen_name}"

    # Optional description links live under profile_bio.entities
    bio = obj.get("profile_bio") or {}
    bio_entities = (bio.get("entities") or {}).get("description") or {}
    desc_links = [
        TextLink(
            url=link.get("expanded_url") or link.get("url") or "",
            text=link.get("display_url"),
            tcourl=link.get("url"),
        )
        for link in (bio_entities.get("urls") or [])
        if link.get("expanded_url") or link.get("url")
    ]

    pinned_ids_raw = obj.get("pinnedTweetIds") or []
    pinned_ids = [_int(x) for x in pinned_ids_raw if _int(x) > 0]

    is_blue = obj.get("isBlueVerified")

    return User(
        id=_int(user_id_str),
        id_str=user_id_str,
        url=profile_url,
        username=screen_name,
        displayname=obj.get("name") or "",
        rawDescription=obj.get("description") or "",
        created=_parse_date(obj.get("createdAt")),
        followersCount=_int(obj.get("followers")),
        friendsCount=_int(obj.get("following")),
        statusesCount=_int(obj.get("statusesCount")),
        favouritesCount=_int(obj.get("favouritesCount")),
        listedCount=0,  # not exposed by twitterapi.io
        mediaCount=_int(obj.get("mediaCount")),
        location=obj.get("location") or "",
        profileImageUrl=obj.get("profilePicture") or "",
        profileBannerUrl=obj.get("coverPicture") or None,
        protected=None,  # twitterapi.io doesn't expose protected state
        verified=is_blue,  # approximate: no legacy-verified flag anymore
        blue=is_blue,
        blueType=obj.get("verifiedType") or None,
        descriptionLinks=desc_links,
        pinnedIds=pinned_ids,
    )


# ---- tweet mapper ----------------------------------------------------------

def to_media(tweet_obj: dict) -> Media:  # noqa: ARG001
    """Build a Media instance from a tweet dict.

    TODO: twitterapi.io's OpenAPI schema does NOT document photo/video URL
    fields on the Tweet object. Live responses may still include
    ``extendedEntities`` / ``extended_entities`` with media URLs — verify
    once an API key is available and extend this function accordingly.
    Until then, return an empty Media so downstream rendering shows no
    media attachment line (rather than crashing).
    """
    return Media(photos=[], videos=[], animated=[])


def _to_user_ref(mention: dict) -> UserRef | None:
    """Turn an entities.user_mentions[i] dict into a UserRef, or None."""
    screen_name = mention.get("screen_name")
    id_str = mention.get("id_str")
    if not screen_name or not id_str:
        return None
    try:
        uid = int(id_str)
    except (TypeError, ValueError):
        return None
    return UserRef(
        id=uid,
        id_str=id_str,
        username=screen_name,
        displayname=mention.get("name") or screen_name,
    )


def _to_text_link(url_obj: dict) -> TextLink | None:
    """Turn an entities.urls[i] dict into a TextLink, or None."""
    expanded = url_obj.get("expanded_url")
    tcourl = url_obj.get("url")
    if not expanded and not tcourl:
        return None
    return TextLink(
        url=expanded or tcourl or "",
        text=url_obj.get("display_url"),
        tcourl=tcourl,
    )


def _reply_user_ref(obj: dict) -> UserRef | None:
    """Build a UserRef from the inReplyToUser* fields twitterapi.io exposes."""
    user_id = obj.get("inReplyToUserId")
    username = obj.get("inReplyToUsername")
    if not user_id or not username:
        return None
    try:
        uid = int(user_id)
    except (TypeError, ValueError):
        return None
    return UserRef(
        id=uid,
        id_str=str(user_id),
        username=username,
        displayname=username,  # display name not provided here
    )


def to_tweet(obj: dict) -> Tweet:
    """Convert a twitterapi.io ``Tweet`` dict to a twscrape ``Tweet``.

    Recurses into ``quoted_tweet`` and ``retweeted_tweet`` which
    twitterapi.io inlines as nested tweet objects — no extra API calls
    needed for quote/retweet expansion.
    """
    author_obj = obj.get("author") or {}
    author = to_user(author_obj)

    tweet_id_str = str(obj.get("id") or "0")
    tweet_url = obj.get("url") or (
        f"https://x.com/{author.username}/status/{tweet_id_str}"
        if author.username
        else f"https://x.com/i/status/{tweet_id_str}"
    )

    entities = obj.get("entities") or {}
    hashtags = [
        h.get("text")
        for h in (entities.get("hashtags") or [])
        if h.get("text")
    ]
    mentioned = [
        ref for ref in (
            _to_user_ref(m) for m in (entities.get("user_mentions") or [])
        ) if ref is not None
    ]
    links = [
        link for link in (
            _to_text_link(u) for u in (entities.get("urls") or [])
        ) if link is not None
    ]

    # Recurse into nested quoted / retweeted objects.
    quoted_obj = obj.get("quoted_tweet")
    retweeted_obj = obj.get("retweeted_tweet")
    quoted = to_tweet(quoted_obj) if isinstance(quoted_obj, dict) else None
    retweeted = to_tweet(retweeted_obj) if isinstance(retweeted_obj, dict) else None

    conv_id_str = str(obj.get("conversationId") or tweet_id_str)

    return Tweet(
        id=_int(tweet_id_str),
        id_str=tweet_id_str,
        url=tweet_url,
        date=_parse_date(obj.get("createdAt")),
        user=author,
        lang=obj.get("lang") or "",
        rawContent=obj.get("text") or "",
        replyCount=_int(obj.get("replyCount")),
        retweetCount=_int(obj.get("retweetCount")),
        likeCount=_int(obj.get("likeCount")),
        quoteCount=_int(obj.get("quoteCount")),
        bookmarkedCount=_int(obj.get("bookmarkCount")),
        conversationId=_int(conv_id_str),
        conversationIdStr=conv_id_str,
        hashtags=hashtags,
        cashtags=[],  # twitterapi.io entities doesn't expose symbols
        mentionedUsers=mentioned,
        links=links,
        media=to_media(obj),
        viewCount=_int_opt(obj.get("viewCount")),
        retweetedTweet=retweeted,
        quotedTweet=quoted,
        place=None,
        coordinates=None,
        inReplyToTweetId=_int_opt(obj.get("inReplyToId")),
        inReplyToTweetIdStr=obj.get("inReplyToId") or None,
        inReplyToUser=_reply_user_ref(obj),
        source=obj.get("source"),
        sourceUrl=None,
        sourceLabel=None,
        card=None,
        possibly_sensitive=obj.get("possiblySensitive"),
    )
