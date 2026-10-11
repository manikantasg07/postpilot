"""Set up GitHub Actions from the browser: sign in, push settings, send secrets."""
from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from agent.config import load_or_default, save_config
from agent.github import api, device, repo_secrets, sync
from agent.github.errors import GitHubError
from agent.llm import KEY_NAMES
from agent.secrets_store import get_secret, set_secret
from web.app import templates
from web.routers.sync import forget_sync_cache
from web.steps import progress

router = APIRouter(prefix="/setup/github")

# Files the app pushes. state.json is missing on purpose: the workflow writes
# that one, so pushing it from here would undo a real run (see agent/github/sync.py).
SYNCED = sync.UP

# One pending sign-in at a time - this is a single-user app on your own machine.
_pending: dict[str, str] = {}


# Pages allowed to ask for the user to be sent back to them after an action.
RETURN_TO = {"/setup/github", "/settings"}


def _back(to: str = "/setup/github", **flash: str) -> RedirectResponse:
    from urllib.parse import urlencode
    to = to if to in RETURN_TO else "/setup/github"       # never redirect anywhere else
    query = urlencode({k: v for k, v in flash.items() if v})
    return RedirectResponse(f"{to}{'?' + query if query else ''}", status_code=303)


def _token() -> str:
    return get_secret("GITHUB_TOKEN") or ""


def _local_secrets(cfg) -> list[str]:
    """The secrets this machine has that Actions also needs."""
    wanted = [KEY_NAMES.get(cfg.llm_provider), "LINKEDIN_ACCESS_TOKEN"]
    return [name for name in wanted if name and get_secret(name)]


def _repo_file(path: str) -> str:
    """Read one of the synced files from disk."""
    return sync.local_text(path)


@router.get("", response_class=HTMLResponse)
def page(request: Request, error: str = "", done: str = "") -> HTMLResponse:
    cfg = load_or_default()
    token = _token()
    login, runs, on_github = "", [], []

    if token:
        try:
            login = api.current_user(token)
            if cfg.github_repo:
                runs = api.recent_runs(cfg.github_repo, token, limit=5)
                on_github = repo_secrets.existing_secrets(cfg.github_repo, token)
        except (GitHubError, OSError) as exc:
            # requests raises OSError subclasses when the network is simply not
            # there - and this is the page you open to debug exactly that.
            error = error or str(exc)

    return templates.TemplateResponse(
        request=request,
        name="pages/setup_github.html",
        context={
            "cfg": cfg,
            "steps": progress("github"),
            "login": login,
            "connected": bool(login),
            "repo": cfg.github_repo or api.repo_from_remote(),
            "detected": api.repo_from_remote(),
            "synced": SYNCED,
            "needed": _local_secrets(cfg),
            "on_github": on_github,
            "runs": runs,
            "error": error,
            "done": done,
        },
    )


@router.post("/client")
def save_client(client_id: str = Form(""), repo: str = Form("")) -> RedirectResponse:
    """Save what was typed. A blank field means "leave it alone", never "erase it".

    The repository field and the Client ID share one form, so clearing the
    stored Client ID just by correcting the repo name would be a nasty surprise.
    """
    current = load_or_default()

    typed = repo.strip()
    wanted = api.normalise_repo(typed) if typed else current.github_repo
    if typed and not wanted:
        return _back(error=f"{typed} does not look like a repository. "
                           "Use owner/repository, or paste the address of the repo on github.com.")

    cfg = dataclasses.replace(
        current,
        github_client_id=client_id.strip() or current.github_client_id,
        github_repo=wanted,
    )
    save_config(cfg)
    return _back(done="Saved.")


@router.post("/start")
def start() -> JSONResponse:
    """Ask GitHub for the code you type on github.com/login/device."""
    cfg = load_or_default()
    try:
        flow = device.start(cfg.github_client_id)
    except GitHubError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    _pending["device_code"] = flow["device_code"]
    return JSONResponse({
        "user_code": flow["user_code"],
        "url": flow["verification_uri"],
        "interval": flow["interval"],
    })


@router.post("/poll")
def poll() -> JSONResponse:
    """Called every few seconds by the page until you approve on GitHub."""
    cfg = load_or_default()
    code = _pending.get("device_code")
    if not code:
        return JSONResponse({"error": "Nothing to wait for. Press Connect again."}, status_code=400)

    try:
        answer = device.poll_once(cfg.github_client_id, code)
    except GitHubError as exc:
        _pending.clear()
        return JSONResponse({"error": str(exc)}, status_code=400)

    if answer.waiting:
        # Tell the page why. Without the reason, "we asked too often" and
        # "he has not approved yet" look identical, and only one of them is
        # fixed by waiting longer.
        return JSONResponse({"status": "waiting", "reason": answer.reason,
                             "slow_down": answer.too_fast})

    token = answer.token

    # GitHub has approved us. Everything after this is local, and a failure
    # here is the worst kind: the sign-in worked, so the page would keep
    # saying "waiting" while nothing was ever going to change. Answer with the
    # reason instead of letting it become a 500 the page cannot read.
    _pending.clear()
    try:
        set_secret("GITHUB_TOKEN", token)
    except Exception as exc:
        return JSONResponse(
            {"error": f"GitHub approved the sign-in, but the token could not be saved: {exc}"},
            status_code=400)

    try:
        login = api.current_user(token)
    except GitHubError as exc:
        return JSONResponse({"error": f"Signed in, but GitHub would not say who: {exc}"},
                            status_code=400)

    return JSONResponse({"status": "connected", "login": login})


@router.post("/push")
def push(next: str = Form("/setup/github")) -> RedirectResponse:
    """Copy the local settings into the repo, one commit per changed file."""
    cfg = load_or_default()
    token = _token()
    if not (token and cfg.github_repo):
        return _back(next, error="Connect GitHub and set the repository first.")

    try:
        changed = push_settings(cfg.github_repo, token)
    except (GitHubError, OSError) as exc:
        return _back(next, error=str(exc))

    forget_sync_cache()
    return _back(next, done=f"Pushed {len(changed)} file(s)." if changed else "Everything already matched.")


def push_settings(repo: str, token: str) -> list[str]:
    """Send every changed settings file up as a single commit.

    One commit, not one per file: config.json and post.yml describe the same
    schedule, and a commit where only one of them has moved is a commit where
    the repository contradicts itself.
    """
    files: dict[str, str] = {}
    for path, _label in SYNCED:
        text = _repo_file(path).replace("\r\n", "\n")       # GitHub keeps LF

        # A missing local file reads as "" - and pushing "" would commit an
        # empty post.yml over the working one, taking the whole schedule with
        # it. Absent means "nothing to say about this file", never "empty it".
        if not text.strip():
            continue

        remote, _ = api.get_file(repo, path, token)
        if not sync.same_text(remote, text):
            files[path] = text

    if files:
        api.put_files(repo, files, "chore: update your schedule and settings from PostCadence", token)
    return sorted(files)


@router.post("/secrets")
def send_secrets() -> RedirectResponse:
    """Encrypt each local secret and store it on the repository."""
    cfg = load_or_default()
    token = _token()
    if not (token and cfg.github_repo):
        return _back(error="Connect GitHub and set the repository first.")

    names = _local_secrets(cfg)
    if not names:
        return _back(error="No secrets stored on this machine yet.")

    try:
        key, key_id = repo_secrets.public_key(cfg.github_repo, token)
        for name in names:
            repo_secrets.put_secret(cfg.github_repo, name, get_secret(name), token, key, key_id)
    except GitHubError as exc:
        return _back(error=str(exc))

    return _back(done=f"Sent {len(names)} secret(s): {', '.join(names)}.")


@router.post("/disconnect")
def disconnect() -> RedirectResponse:
    from agent.secrets_store import delete_secret
    delete_secret("GITHUB_TOKEN")
    _pending.clear()
    return _back(done="Disconnected from GitHub on this machine.")
