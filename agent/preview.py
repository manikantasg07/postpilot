"""Draft previews as GitHub Issues, so you can approve from your phone."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

API = "https://api.github.com"
START = "<!-- post:start -->"
END = "<!-- post:end -->"
BODY = re.compile(re.escape(START) + r"(.*?)" + re.escape(END), re.DOTALL)
APPROVE = re.compile(r"^\s*/approve\b", re.MULTILINE)
CANCEL = re.compile(r"^\s*/cancel\b", re.MULTILINE)
LABEL = "postcadence-draft"
TOPIC = re.compile(r"\*\*Topic:\*\*(.+)")
TITLE = re.compile(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})")


class PreviewError(RuntimeError):
    """Something went wrong talking to the GitHub API."""


@dataclass
class Draft:
    number: int
    text: str
    decision: str          # approve / cancel / none
    created_at: str = ""   # when the issue was opened, from GitHub (UTC)

    def minutes_open(self, now: datetime | None = None) -> float:
        """How long you have had to look at it.

        The review window is measured from this, not from the clock, because
        GitHub can start a scheduled run an hour or more late.
        """
        if not self.created_at:
            return 0.0
        opened = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        now = now or datetime.now(timezone.utc)
        return (now - opened).total_seconds() / 60


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise PreviewError(f"{name} is not set. Preview mode only works inside GitHub Actions.")
    return value


def _call(method: str, path: str, token: str, **kwargs) -> dict | list:
    response = requests.request(
        method,
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=30,
        **kwargs,
    )
    if response.status_code >= 300:
        raise PreviewError(f"GitHub API {method} {path} failed ({response.status_code}).")
    return response.json()


def title_for(date: str, slot: str) -> str:
    return f"Draft post for {date} {slot}"


def build_body(topic: str, text: str, slot: str, timeout_action: str, minutes: int,
               mention: str = "") -> str:
    fallback = "publish it anyway" if timeout_action == "post" else "skip this slot"
    greeting = f"@{mention} a post is ready for you.\n\n" if mention else ""
    return (
        f"{greeting}"
        f"**Topic:** {topic}\n\n"
        f"Comment `/approve` to publish, or `/cancel` to skip.\n"
        f"You can also edit the text below and it will publish as edited.\n"
        f"If nothing happens within about {minutes} minutes, the agent will {fallback} at {slot}.\n\n"
        f"---\n\n{START}\n{text}\n{END}\n"
    )


def extract_text(body: str) -> str:
    """Pull the post out of an issue body, so edits between the markers count."""
    match = BODY.search(body or "")
    if not match:
        raise PreviewError("Could not find the post markers in the issue body.")
    return match.group(1).strip()


def decide(comments: list[dict]) -> str:
    """Newest instruction wins, so a /cancel after an /approve is respected."""
    for comment in reversed(comments):
        text = comment.get("body", "")
        if CANCEL.search(text):
            return "cancel"
        if APPROVE.search(text):
            return "approve"
    return "none"


def open_draft(date: str, slot: str, token: str = "", repo: str = "") -> Draft | None:
    """Find today's open draft for this slot, with its current text and decision."""
    token = token or _env("GITHUB_TOKEN")
    repo = repo or _env("GITHUB_REPOSITORY")

    issues = _call("GET", f"/repos/{repo}/issues?state=open&labels={LABEL}&per_page=50", token)
    wanted = title_for(date, slot)
    match = next((issue for issue in issues if issue.get("title") == wanted), None)
    if match is None:
        return None

    comments = _call("GET", f"/repos/{repo}/issues/{match['number']}/comments?per_page=100", token)
    return Draft(number=match["number"], text=extract_text(match.get("body", "")),
                 decision=decide(comments), created_at=match.get("created_at", ""))


def owner_of(repo: str) -> str:
    """The account the repository belongs to - "sud018" from "sud018/agent"."""
    return repo.split("/")[0]


@dataclass
class RemoteDraft:
    """An open draft issue, as the app's Drafts page needs to show it."""

    number: int
    date: str
    slot: str
    topic: str
    text: str
    decision: str
    created_at: str
    url: str

    def minutes_open(self, now: datetime | None = None) -> float:
        if not self.created_at:
            return 0.0
        opened = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        return ((now or datetime.now(timezone.utc)) - opened).total_seconds() / 60


def topic_of(body: str) -> str:
    match = TOPIC.search(body or "")
    return match.group(1).strip() if match else ""


def when_of(title: str) -> tuple[str, str]:
    """"Draft post for 2026-09-29 17:40" -> ("2026-09-29", "17:40")."""
    match = TITLE.search(title or "")
    return (match.group(1), match.group(2)) if match else ("", "")


def open_drafts(token: str = "", repo: str = "") -> list[RemoteDraft]:
    """Every draft issue still waiting, newest first.

    The app could not see these at all: drafts written by GitHub lived only in
    the Issues tab, so the Drafts page showed an empty list while a post was
    sitting there waiting to be read.
    """
    token = token or _env("GITHUB_TOKEN")
    repo = repo or _env("GITHUB_REPOSITORY")

    issues = _call("GET", f"/repos/{repo}/issues?state=open&labels={LABEL}&per_page=50", token)
    found = []
    for issue in issues:
        date, slot = when_of(issue.get("title", ""))
        body = issue.get("body", "")
        try:
            text = extract_text(body)
        except PreviewError:
            continue                      # not one of ours after all
        comments = _call("GET", f"/repos/{repo}/issues/{issue['number']}/comments?per_page=100",
                         token)
        found.append(RemoteDraft(
            number=issue["number"], date=date, slot=slot, topic=topic_of(body), text=text,
            decision=decide(comments), created_at=issue.get("created_at", ""),
            url=issue.get("html_url", ""),
        ))
    return sorted(found, key=lambda d: (d.date, d.slot), reverse=True)


def set_draft_text(number: int, text: str, token: str = "", repo: str = "") -> None:
    """Replace the post inside an issue, leaving the instructions around it."""
    token = token or _env("GITHUB_TOKEN")
    repo = repo or _env("GITHUB_REPOSITORY")

    issue = _call("GET", f"/repos/{repo}/issues/{number}", token)
    body = issue.get("body", "")
    if not BODY.search(body):
        raise PreviewError("Could not find the post markers in the issue body.")

    replaced = BODY.sub(lambda _: f"{START}\n{text.strip()}\n{END}", body, count=1)
    _call("PATCH", f"/repos/{repo}/issues/{number}", token, json={"body": replaced})


def say(number: int, words: str, token: str = "", repo: str = "") -> None:
    """Add a comment - how /approve and /cancel are given from this app."""
    token = token or _env("GITHUB_TOKEN")
    repo = repo or _env("GITHUB_REPOSITORY")
    _call("POST", f"/repos/{repo}/issues/{number}/comments", token, json={"body": words})


def create_draft(date: str, slot: str, topic: str, text: str, timeout_action: str,
                 minutes: int, token: str = "", repo: str = "") -> int:
    token = token or _env("GITHUB_TOKEN")
    repo = repo or _env("GITHUB_REPOSITORY")

    # Assign it to you, and mention you in the body.
    #
    # GitHub Mobile only pushes for a fixed set of categories - mentions,
    # assignments, review requests, workflow runs. "An issue was opened in a
    # repository you watch" is not one of them, so a draft that is merely
    # labelled reaches the Issues tab and nothing else. Assigning it makes it
    # an "Assigned" notification, which the app does push; the @mention is a
    # second route for anyone whose assignment pushes are off.
    #
    # Both sit above the post markers, so neither reaches LinkedIn.
    you = owner_of(repo)

    issue = _call("POST", f"/repos/{repo}/issues", token, json={
        "title": title_for(date, slot),
        "body": build_body(topic, text, slot, timeout_action, minutes, mention=you),
        "labels": [LABEL],
        "assignees": [you],
    })
    return issue["number"]


def close_draft(number: int, comment: str, token: str = "", repo: str = "") -> None:
    token = token or _env("GITHUB_TOKEN")
    repo = repo or _env("GITHUB_REPOSITORY")
    _call("POST", f"/repos/{repo}/issues/{number}/comments", token, json={"body": comment})
    _call("PATCH", f"/repos/{repo}/issues/{number}", token, json={"state": "closed"})
