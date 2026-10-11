"""Publish a text post to LinkedIn."""

from __future__ import annotations

import re

import requests

from agent.linkedin.errors import LinkedInAuthError, LinkedInError

POSTS_URL = "https://api.linkedin.com/rest/posts"
LINKEDIN_VERSION = "202608"
MAX_LENGTH = 3000

# The `commentary` field is not plain text: LinkedIn parses it as "Little Text
# Format", where these characters mark up mentions, links and entities. Every
# one of them has to arrive backslash-escaped even when we mean it literally.
# Unescaped, LinkedIn stops reading at the first one - which is why a post
# ending "... generation (RAG) still necessary?" published as "... generation"
# and nothing else. The docs are explicit: "All reserved characters need to be
# escaped with a backslash, even if those characters are not used in one of the
# supported elements or templates."
RESERVED = r"|{}@[]()<>#\*_~"
_ESCAPE = re.compile("([" + re.escape(RESERVED) + "])")

# A hashtag is the one place we *want* LinkedIn's own parsing: escaping the '#'
# would publish "\#RAG" as dead text instead of a tag people can follow. The
# formatter appends these deliberately, so hashtag words pass through whole -
# including any '_' inside them, which is a legal hashtag character.
_HASHTAG = re.compile(r"#[A-Za-z0-9_]+")


def escape(text: str) -> str:
    """Escape Little Text Format characters, leaving real hashtags clickable."""
    out: list[str] = []
    position = 0
    for tag in _HASHTAG.finditer(text):
        out.append(_ESCAPE.sub(r"\\\1", text[position:tag.start()]))
        out.append(tag.group(0))
        position = tag.end()
    out.append(_ESCAPE.sub(r"\\\1", text[position:]))
    return "".join(out)


def post_text(text: str, access_token: str, member_id: str) -> str:
    """Publish `text`. Returns the post's URN."""

    text = text.strip()
    if not text:
        raise LinkedInError("Refusing to post empty text.")
    # Counted before escaping: the backslashes are markup, not characters
    # anybody reads, and LinkedIn's 3000 applies to the post as it appears.
    if len(text) > MAX_LENGTH:
        raise LinkedInError(f"Post is {len(text)} characters; LinkedIn allows {MAX_LENGTH}.")

    headers = {
        "Authorization": f"Bearer {access_token}",
        "LinkedIn-Version": LINKEDIN_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }

    body = {
        "author": f"urn:li:person:{member_id}",
        "commentary": escape(text),
        "visibility": "PUBLIC",
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }

    response = requests.post(POSTS_URL, headers=headers, json=body, timeout=30)

    if response.status_code == 401:
        raise LinkedInAuthError("LinkedIn rejected the token. Run: python -m agent linkedin connect")
    if response.status_code not in (200, 201):
        raise LinkedInError(f"Post failed ({response.status_code}): {response.text[:300]}")

    return response.headers.get("x-restli-id", "")
