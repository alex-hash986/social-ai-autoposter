"""Text conventions shared by TenChat draft generation and sandbox export."""

import re


def topic_hashtag(topic: str) -> str:
    """Create a compact Cyrillic-friendly tag from the post topic."""
    words = re.findall(r"[^\W_]+", topic.casefold())
    tag = "".join(words[:4])[:48]
    return f"#{tag or 'деловаяпрактика'}"
