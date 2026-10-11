"""Punctuality: let an outside clock start the workflow when GitHub's is late.

There are two ways to use this page. Either copy the four values into a
scheduling service by hand, or hand the app an API key and let it own the
service's jobs outright. The second removes a third copy of the schedule that
nothing was checking - see agent/cronjob/sync.py.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from agent.config import load_or_default
from agent.cronjob import sync
from agent.cronjob.errors import CronJobBusyError, CronJobError
from agent.github import dispatch
from agent.github.errors import GitHubError
from agent.schedule import trigger_times
from agent.secrets_store import delete_secret, get_secret, set_secret
from web.app import templates
from web.health import forget_trigger_cache

router = APIRouter(prefix="/setup/trigger")

TOKEN_SCOPE = "Actions: Read and write"


def _secret(name: str) -> str:
    try:
        return get_secret(name) or ""
    except Exception:              # a locked or missing keyring is just "no secret"
        return ""


def _token() -> str:
    """The token used for the app's own test button."""
    return _secret("GITHUB_TOKEN")


def _automatic() -> tuple[str, str]:
    """The pair needed to manage cron-job.org: their key, and the token to send."""
    return _secret("CRONJOB_API_KEY"), _secret("GITHUB_DISPATCH_TOKEN")


def _back(**flash: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in flash.items() if v})
    return RedirectResponse(f"/setup/trigger{'?' + query if query else ''}", status_code=303)


@router.get("", response_class=HTMLResponse)
def page(request: Request, error: str = "", done: str = "") -> HTMLResponse:
    cfg = load_or_default()
    repo = cfg.github_repo
    key, dispatch_token = _automatic()

    jobs, listing_error = [], ""
    if key:
        try:
            jobs = sorted(sync.mine(sync.api.list_jobs(key)))
        except CronJobBusyError:
            listing_error = ("cron-job.org is rate limiting us, so this list is not "
                             "loading. Your cronjobs are unaffected - wait a minute "
                             "and reload.")
        except (CronJobError, OSError) as exc:
            listing_error = str(exc)

    return templates.TemplateResponse(request, "pages/setup_trigger.html", {
        "cfg": cfg,
        "repo": repo,
        "connected": bool(repo and _token()),
        "url": dispatch.url(repo or "YOUR-NAME/YOUR-REPO"),
        "headers": dispatch.HEADERS,
        # Pretty-printed so it can be pasted into a form field and still read.
        "body": json.dumps(dispatch.body(), indent=2),
        "times": trigger_times(cfg),
        "token_scope": TOKEN_SCOPE,
        "automatic": bool(key and dispatch_token),
        "has_key": bool(key),
        "has_dispatch_token": bool(dispatch_token),
        "jobs": jobs,
        "listing_error": listing_error,
        "error": error,
        "done": done,
    })


@router.post("/test")
def test() -> RedirectResponse:
    """Fire one dry run, so a mistake is found now rather than at 09:00.

    Dry run means the model still writes a post and the run still shows up in
    the Actions tab, but nothing reaches LinkedIn. That is exactly the proof
    wanted here: the trigger works, and it cost nothing.
    """
    cfg = load_or_default()
    token = _token()
    if not (cfg.github_repo and token):
        return _back(error="Connect GitHub first - this page needs the repo and a token.")

    try:
        dispatch.run_now(cfg.github_repo, token, dry_run=True)
    except GitHubError as exc:
        return _back(error=str(exc))

    return _back(done="Sent. It should appear in the Actions tab within a few seconds.")


@router.post("/connect")
def connect(api_key: str = Form(""), dispatch_token: str = Form("")) -> RedirectResponse:
    """Store the two secrets, then immediately prove the key works.

    Blank fields mean "leave that one alone", so the page can be re-submitted
    to change one without retyping the other.
    """
    cfg = load_or_default()
    if not cfg.github_repo:
        return _back(error="Set your repository on the GitHub step first.")

    for name, value in (("CRONJOB_API_KEY", api_key),
                        ("GITHUB_DISPATCH_TOKEN", dispatch_token)):
        if value.strip():
            set_secret(name, value)

    key, token = _automatic()
    if not (key and token):
        missing = "an API key" if not key else "a dispatch token"
        return _back(error=f"Still need {missing} before the app can manage your cronjobs.")

    try:
        count = sync.api.whoami(key)
    except CronJobError as exc:
        return _back(error=str(exc))

    # A new token only reaches the service by rewriting the jobs that carry it.
    forget_trigger_cache()
    return _back(done=f"Connected. Your account has {count} cronjob(s). Press Sync to set them up.")


@router.post("/sync")
def synchronise() -> RedirectResponse:
    """Make cron-job.org match the schedule. Safe to press at any time."""
    cfg = load_or_default()
    key, token = _automatic()
    if not (key and token and cfg.github_repo):
        return _back(error="Connect cron-job.org first.")

    try:
        # rewrite: the GitHub token lives in each job's headers, and nothing
        # compares those. Pressing Sync is the moment to push it out again.
        plan = sync.apply(cfg, cfg.github_repo, token, key, rewrite=True)
    except CronJobError as exc:
        return _back(error=str(exc))

    forget_trigger_cache()
    return _back(done=f"Schedule pushed to cron-job.org. {plan.summary()}")


@router.post("/disconnect")
def disconnect() -> RedirectResponse:
    """Remove our cronjobs, then forget the keys.

    In that order: keys first would leave jobs running that nothing can reach,
    firing on a schedule nobody can change from here.
    """
    key, _ = _automatic()
    removed = 0
    if key:
        try:
            removed = sync.remove_all(key)
        except CronJobError as exc:
            return _back(error=f"Left everything in place: {exc}")

    delete_secret("CRONJOB_API_KEY")
    delete_secret("GITHUB_DISPATCH_TOKEN")
    forget_trigger_cache()
    return _back(done=f"Disconnected and removed {removed} cronjob(s).")
