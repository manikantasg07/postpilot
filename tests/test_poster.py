"""LinkedIn reads `commentary` as markup, so everything literal must be escaped.

Unescaped, LinkedIn stopped reading at the first reserved character and
published only the opening fragment of a post.
"""
from __future__ import annotations

import pytest

from agent.linkedin import poster
from agent.linkedin.errors import LinkedInError


class FakeResponse:
    def __init__(self, status_code=201, headers=None):
        self.status_code = status_code
        self.headers = headers or {"x-restli-id": "urn:li:share:1"}
        self.text = ""


@pytest.fixture
def sent(monkeypatch):
    """Capture the body we would have sent instead of calling LinkedIn."""
    captured: dict = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(json)
        return FakeResponse()

    monkeypatch.setattr(poster.requests, "post", fake_post)
    return captured


def test_the_bug_brackets_survive(sent):
    poster.post_text("Is retriever-augmented generation (RAG) still needed?", "t", "m")
    assert sent["commentary"] == r"Is retriever-augmented generation \(RAG\) still needed?"


@pytest.mark.parametrize("char", list("|{}@[]()<>*_~"))
def test_every_reserved_character_is_escaped(char, sent):
    poster.post_text(f"before {char} after", "t", "m")
    assert sent["commentary"] == f"before \\{char} after"


def test_backslash_is_escaped_once_not_twice(sent):
    poster.post_text(r"a \ b", "t", "m")
    assert sent["commentary"] == r"a \\ b"


def test_a_stray_hash_is_escaped(sent):
    poster.post_text("costs # per token", "t", "m")
    assert sent["commentary"] == r"costs \# per token"


def test_hashtags_stay_clickable(sent):
    poster.post_text("Shipped it.\n\n#RAG #LLMOps #Context_Windows", "t", "m")
    assert sent["commentary"] == "Shipped it.\n\n#RAG #LLMOps #Context_Windows"


def test_hashtags_and_escaping_together(sent):
    poster.post_text("Bigger windows (finally).\n\n#RAG", "t", "m")
    assert sent["commentary"] == "Bigger windows \\(finally\\).\n\n#RAG"


def test_plain_text_is_untouched(sent):
    poster.post_text("No reserved characters here at all.", "t", "m")
    assert sent["commentary"] == "No reserved characters here at all."


def test_length_is_judged_before_escaping(monkeypatch):
    """Escaping must not push a legal post over LinkedIn's limit."""
    monkeypatch.setattr(poster.requests, "post",
                        lambda *a, **k: FakeResponse())
    text = "(" * (poster.MAX_LENGTH - 1)      # every character needs a backslash
    assert poster.post_text(text, "t", "m")   # would be 2x the limit if counted after

    with pytest.raises(LinkedInError, match="characters"):
        poster.post_text("x" * (poster.MAX_LENGTH + 1), "t", "m")
