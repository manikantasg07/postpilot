"""Write a post, look at it, change it, then publish it - all before it goes live."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agent import drafts as book
from agent import preview, run
from agent.config import load_or_default
from agent.linkedin.errors import LinkedInError
from agent.llm.base import LLMError
from agent.preview import PreviewError
from agent.secrets_store import get_secret
from agent.status import link_for_id
from agent.topics.loader import TopicError
from web.app import templates

router = APIRouter(prefix="/drafts")

SEE_MORE = 210      # LinkedIn hides the rest behind "see more" at roughly this point
MAX_LENGTH = 3000   # LinkedIn's own limit


def _back(**flash: str) -> RedirectResponse:
    """Post/Redirect/Get: a refresh must never re-run an action."""
    query = urlencode({k: v for k, v in flash.items() if v})
    return RedirectResponse(f"/drafts{'?' + query if query else ''}", status_code=303)


def _connected(cfg) -> bool:
    try:
        return bool(cfg.linkedin_member_id and get_secret("LINKEDIN_ACCESS_TOKEN"))
    except Exception:       # keyring missing or locked - treat as not connected
        return False


def _github() -> tuple[str, str]:
    """The token and repo needed to read the draft issues, or ("", "")."""
    cfg = load_or_default()
    try:
        return (get_secret("GITHUB_TOKEN") or ""), cfg.github_repo
    except Exception:
        return "", cfg.github_repo


def _waiting() -> tuple[list, str]:
    """Drafts GitHub is holding. Never fatal: this page must open regardless."""
    token, repo = _github()
    if not (token and repo):
        return [], ""
    try:
        return preview.open_drafts(token, repo), ""
    except (PreviewError, OSError) as exc:
        return [], str(exc)


@router.get("", response_class=HTMLResponse)
def page(request: Request, posted: str = "", error: str = "", saved: str = "",
         skipped: str = "") -> HTMLResponse:
    cfg = load_or_default()
    waiting, waiting_error = _waiting()
    return templates.TemplateResponse(
        request=request,
        name="pages/drafts.html",
        context={
            "drafts": list(reversed(book.load_drafts())),   # newest first
            "waiting": waiting,
            "waiting_error": waiting_error,
            "cfg": cfg,
            "slots": cfg.post_times,
            "today": run.today_in(cfg),
            "connected": _connected(cfg),
            "see_more": SEE_MORE,
            "max_length": MAX_LENGTH,
            "posted": posted,
            "post_url": link_for_id(posted),
            "error": error,
            "saved": saved,
            "skipped": skipped,
        },
    )


@router.post("/new")
def new(slot: str = Form("")) -> RedirectResponse:
    """Ask the model for a post. The topic bookmark does not move yet."""
    cfg = load_or_default()
    slot = slot or (cfg.post_times[0] if cfg.post_times else "09:00")
    try:
        pick, text = run.compose(cfg, slot)
    except (LLMError, TopicError) as exc:
        return _back(error=str(exc))

    book.add(book.Draft(date=run.today_in(cfg), slot=slot, topic=pick.topic, text=text,
                        topic_index=pick.index, is_repeat=pick.is_repeat))
    return _back()


@router.post("/{draft_id}/save")
def save(draft_id: str, text: str = Form(...)) -> RedirectResponse:
    if not text.strip():
        return _back(error="A post cannot be empty.")
    if len(text) > MAX_LENGTH:
        return _back(error=f"LinkedIn allows {MAX_LENGTH} characters; this is {len(text)}.")
    if book.set_text(draft_id, text) is None:
        return _back(error="That draft is no longer here.")
    return _back(saved="1")


@router.post("/{draft_id}/rewrite")
def rewrite(draft_id: str) -> RedirectResponse:
    """Throw this attempt away and ask the model again for the same slot."""
    draft = book.find(draft_id)
    if draft is None:
        return _back(error="That draft is no longer here.")

    cfg = load_or_default()
    try:
        _, text = run.compose(cfg, draft.slot)
    except (LLMError, TopicError) as exc:
        return _back(error=str(exc))

    book.set_text(draft_id, text)
    return _back(saved="1")


@router.post("/{draft_id}/post")
def publish(draft_id: str) -> RedirectResponse:
    draft = book.find(draft_id)
    if draft is None:
        return _back(error="That draft is no longer here.")

    cfg = load_or_default()
    try:
        post_id = run.publish(cfg, draft.pick, draft.slot, draft.text, draft.date)
    except LinkedInError as exc:
        return _back(error=str(exc))

    book.remove(draft_id)
    return _back(posted=post_id)


@router.post("/{draft_id}/skip")
def skip(draft_id: str) -> RedirectResponse:
    """Not this one. The slot is marked handled so nothing posts for it today."""
    draft = book.find(draft_id)
    if draft is None:
        return _back(error="That draft is no longer here.")

    run.record_skip(load_or_default(), draft.pick, draft.slot, draft.date)
    book.remove(draft_id)
    return _back(skipped="1")


@router.post("/{draft_id}/discard")
def discard(draft_id: str) -> RedirectResponse:
    """Delete the draft and leave no trace - the slot can still be used."""
    book.remove(draft_id)
    return _back()


# --- drafts GitHub is holding ------------------------------------------------
#
# These live in the Issues tab, not in data/drafts.json, so the app used to be
# blind to them: a post could be sitting there waiting to be read while this
# page showed nothing at all. Each action here is the same one you would take
# on GitHub, so the two views never disagree.


def _remote(number: int, action, success: str) -> RedirectResponse:
    token, repo = _github()
    if not (token and repo):
        return _back(error="Connect GitHub to act on drafts it is holding.")
    try:
        action(number, token, repo)
    except (PreviewError, OSError) as exc:
        return _back(error=str(exc))
    return _back(saved=success)


@router.post("/waiting/{number}/approve")
def approve_waiting(number: int) -> RedirectResponse:
    """Comment /approve, exactly as you would on your phone.

    It does not publish from here. Publishing is the scheduled run's job - it
    owns the history file, and a post made here would be invisible to it.
    """
    return _remote(number, lambda n, t, r: preview.say(n, "/approve", t, r),
                   "Approved. The next run publishes it.")


@router.post("/waiting/{number}/cancel")
def cancel_waiting(number: int) -> RedirectResponse:
    return _remote(number, lambda n, t, r: preview.say(n, "/cancel", t, r),
                   "Cancelled. Nothing will be posted for that slot.")


@router.post("/waiting/{number}/save")
def save_waiting(number: int, text: str = Form(...)) -> RedirectResponse:
    """Edit the post inside the issue, without touching the instructions."""
    words = text.strip()
    if not words:
        return _back(error="A draft cannot be empty.")
    if len(words) > MAX_LENGTH:
        return _back(error=f"LinkedIn stops at {MAX_LENGTH} characters; this is {len(words)}.")

    return _remote(number, lambda n, t, r: preview.set_draft_text(n, words, t, r),
                   "Saved to GitHub. The run publishes what is there now.")
