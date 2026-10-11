"""Setup step 3: how often and when to post - and keep post.yml in step."""
from __future__ import annotations

from dataclasses import replace
from zoneinfo import available_timezones

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from agent.config import MAX_POSTS_PER_DAY, ConfigError, load_or_default, save_config
from agent.cronjob import sync as cronjob_sync
from agent.cronjob.errors import CronJobError
from agent.github.errors import GitHubError
from agent.schedule import cron_lines
from agent.secrets_store import get_secret
from web.app import templates
from web.health import forget_trigger_cache
from web.routers.github import push_settings
from web.routers.sync import forget_sync_cache
from web.steps import progress
from web.workflow import WorkflowError, apply

router = APIRouter(prefix="/setup/schedule")

COMMON_ZONES = [
    "America/Los_Angeles", "America/Denver", "America/Chicago", "America/New_York",
    "Europe/London", "Europe/Berlin", "Asia/Kolkata", "Asia/Singapore", "Australia/Sydney",
]


def _zones() -> list[str]:
    rest = sorted(z for z in available_timezones() if "/" in z and z not in COMMON_ZONES)
    return COMMON_ZONES + rest


def _render(request: Request, cfg, errors: list[str] | None = None,
            saved: bool = False, changed: bool = False, pushed: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="pages/setup_schedule.html",
        context={
            "steps": progress("schedule"),
            "cfg": cfg,
            "zones": _zones(),
            "max_posts": MAX_POSTS_PER_DAY,
            "cron": cron_lines(cfg) if not errors else [],
            "errors": errors or [],
            "saved": saved,
            "changed": changed,
            "pushed": pushed,
        },
    )


@router.get("", response_class=HTMLResponse)
def page(request: Request) -> HTMLResponse:
    return _render(request, load_or_default())


@router.post("", response_class=HTMLResponse)
def save(
    request: Request,
    post_times: list[str] = Form(...),
    timezone: str = Form(...),
    mode: str = Form("auto"),
    preview_minutes: int = Form(30),
    preview_timeout_action: str = Form("post"),
    catch_up_hours: int = Form(6),
) -> HTMLResponse:
    times = sorted({t.strip() for t in post_times if t.strip()})
    candidate = replace(
        load_or_default(),
        posts_per_day=len(times),
        post_times=times,
        timezone=timezone.strip(),
        mode=mode,
        preview_minutes=preview_minutes,
        preview_timeout_action=preview_timeout_action,
        catch_up_hours=catch_up_hours,
    )

    try:
        save_config(candidate)                       # validates before writing
    except ConfigError as exc:
        lines = [line.strip(" -") for line in str(exc).splitlines()[1:]]
        return _render(request, candidate, errors=lines)

    try:
        _, changed = apply(candidate)                # keep post.yml in step
    except WorkflowError as exc:
        return _render(request, candidate, errors=[str(exc)])

    # A schedule that lives in four places is only correct when all four
    # agree, so saving updates all four. Nothing here can undo the save: each
    # step reports its own failure and the next one still runs.
    pushed = " ".join(filter(None, [_push_to_github(candidate), _push_to_service(candidate)]))

    return _render(request, candidate, saved=True, changed=changed, pushed=pushed)


def _push_to_github(cfg) -> str:
    """Send config.json, topics.json and post.yml up, as one commit.

    Actions runs the code that is in the repo and reads the schedule from the
    repo, so a schedule that stays on this laptop changes nothing at all. This
    used to be a separate button, which meant the most important step was the
    one easiest to forget.
    """
    try:
        token = get_secret("GITHUB_TOKEN") or ""
    except Exception:
        return ""
    if not (token and cfg.github_repo):
        return ""

    try:
        sent = push_settings(cfg.github_repo, token)
    except (GitHubError, OSError) as exc:
        return f"GitHub was not updated: {exc}"

    forget_sync_cache()
    return f"Pushed {len(sent)} file(s) to GitHub." if sent else "GitHub already matched."


def _push_to_service(cfg) -> str:
    """Keep cron-job.org in step, if the app has been given the keys.

    A failure here is reported, never raised: the schedule itself saved fine,
    and losing that because a third party was briefly unreachable would be
    worse than being late for one day.
    """
    try:
        key = get_secret("CRONJOB_API_KEY") or ""
        token = get_secret("GITHUB_DISPATCH_TOKEN") or ""
    except Exception:
        return ""

    if not (key and token and cfg.github_repo):
        return ""

    try:
        summary = cronjob_sync.apply(cfg, cfg.github_repo, token, key).summary()
        forget_trigger_cache()
        return "cron-job.org: " + summary
    except CronJobError as exc:
        return f"cron-job.org was not updated: {exc}"
