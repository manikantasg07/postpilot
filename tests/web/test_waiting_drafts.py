"""Drafts GitHub is holding, shown and acted on from the app.

Before this, a draft written by the scheduled run existed only in the Issues
tab. The Drafts page showed "No drafts yet" while a post sat there waiting to
be read, which is the one moment the page exists for.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from agent.config import load_config, save_config
from agent.preview import PreviewError, RemoteDraft
from web.app import app
from web.routers import drafts as page

client = TestClient(app)


def waiting(minutes: float = 8, decision: str = "none") -> RemoteDraft:
    opened = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return RemoteDraft(number=7, date="2026-09-29", slot="17:40", topic="Evaluating RAG",
                       text="A post about evaluating retrieval.", decision=decision,
                       created_at=opened.isoformat().replace("+00:00", "Z"),
                       url="https://github.com/me/agent/issues/7")


@pytest.fixture
def connected(monkeypatch):
    save_config(dataclasses.replace(load_config(), github_repo="me/agent",
                                    mode="preview", preview_minutes=30))
    monkeypatch.setattr(page, "_github", lambda: ("gho_test", "me/agent"))


def test_a_draft_on_github_appears_in_the_app(connected, monkeypatch):
    monkeypatch.setattr(page.preview, "open_drafts", lambda token, repo: [waiting()])

    body = client.get("/drafts").text

    assert "Waiting on GitHub" in body
    assert "issue #7" in body
    assert "A post about evaluating retrieval." in body


def test_it_shows_how_long_you_have_left(connected, monkeypatch):
    monkeypatch.setattr(page.preview, "open_drafts", lambda token, repo: [waiting(minutes=8)])
    assert "22 min of review time left" in client.get("/drafts").text


def test_an_expired_window_says_what_happens_next(connected, monkeypatch):
    monkeypatch.setattr(page.preview, "open_drafts", lambda token, repo: [waiting(minutes=45)])
    body = client.get("/drafts").text
    assert "review time is up" in body and "posts on the next run" in body


def test_an_approved_draft_says_so(connected, monkeypatch):
    monkeypatch.setattr(page.preview, "open_drafts",
                        lambda token, repo: [waiting(decision="approve")])
    assert "approved" in client.get("/drafts").text


def test_the_page_still_opens_when_github_cannot_be_read(connected, monkeypatch):
    """This page must never fail because a third party did."""
    def boom(token, repo):
        raise PreviewError("GitHub API GET /issues failed (503)")

    monkeypatch.setattr(page.preview, "open_drafts", boom)

    response = client.get("/drafts")
    assert response.status_code == 200
    assert "503" in response.text


def test_nothing_is_fetched_without_a_connection(monkeypatch):
    monkeypatch.setattr(page, "_github", lambda: ("", ""))
    monkeypatch.setattr(page.preview, "open_drafts",
                        lambda *a, **k: pytest.fail("should not reach GitHub"))
    assert client.get("/drafts").status_code == 200


# --- acting on one -----------------------------------------------------------

def test_approving_comments_rather_than_publishing(connected, monkeypatch):
    """Publishing belongs to the scheduled run: it owns the history file, and a
    post made here would be invisible to it and published all over again."""
    said = {}
    monkeypatch.setattr(page.preview, "say",
                        lambda n, words, t, r: said.update(number=n, words=words))
    monkeypatch.setattr(page.run, "publish", lambda *a, **k: pytest.fail("must not publish here"))

    response = client.post("/drafts/waiting/7/approve", follow_redirects=False)

    assert said == {"number": 7, "words": "/approve"}
    assert "saved=" in response.headers["location"]


def test_cancelling_comments_too(connected, monkeypatch):
    said = {}
    monkeypatch.setattr(page.preview, "say",
                        lambda n, words, t, r: said.update(words=words))
    client.post("/drafts/waiting/7/cancel", follow_redirects=False)
    assert said["words"] == "/cancel"


def test_editing_writes_the_new_text_into_the_issue(connected, monkeypatch):
    saved = {}
    monkeypatch.setattr(page.preview, "set_draft_text",
                        lambda n, text, t, r: saved.update(number=n, text=text))

    client.post("/drafts/waiting/7/save", data={"text": "  my own words  "},
                follow_redirects=False)

    assert saved == {"number": 7, "text": "my own words"}


def test_an_empty_edit_is_refused(connected, monkeypatch):
    monkeypatch.setattr(page.preview, "set_draft_text",
                        lambda *a, **k: pytest.fail("should not reach GitHub"))
    response = client.post("/drafts/waiting/7/save", data={"text": "   "},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_an_overlong_edit_is_refused(connected, monkeypatch):
    monkeypatch.setattr(page.preview, "set_draft_text",
                        lambda *a, **k: pytest.fail("should not reach GitHub"))
    response = client.post("/drafts/waiting/7/save", data={"text": "x" * 3001},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert "3000" in response.headers["location"]


def test_acting_without_a_connection_is_explained(monkeypatch):
    monkeypatch.setattr(page, "_github", lambda: ("", ""))
    response = client.post("/drafts/waiting/7/approve", follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_a_github_failure_is_reported_not_swallowed(connected, monkeypatch):
    def boom(n, words, t, r):
        raise PreviewError("GitHub API POST /comments failed (403)")

    monkeypatch.setattr(page.preview, "say", boom)
    response = client.post("/drafts/waiting/7/approve", follow_redirects=False)
    assert "403" in response.headers["location"]
