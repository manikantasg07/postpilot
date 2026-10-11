"""Make cron-job.org match the schedule, whatever state it is in now.

This is a reconciler, not a script of steps. It reads what the account
currently has, works out what it should have, and applies the difference. Run
it twice and the second run does nothing; run it against a half-finished first
attempt and it finishes the job. That property is what lets the app call it on
every schedule save without anyone having to remember what was done before.

Only jobs this app created are touched. They are recognised by a title prefix,
so somebody else's cronjobs on the same account are never read as ours and
never deleted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from agent.config import Config
from agent.cronjob import api
from agent.github import dispatch
from agent.schedule import trigger_times

# Every job we own is titled "PostCadence · HH:MM". The separator is unusual on
# purpose: a job somebody named "PostCadence backup" must not look like ours.
PREFIX = "PostCadence · "


def title_for(moment: str) -> str:
    return f"{PREFIX}{moment}"


def moment_of(job: dict) -> str:
    """The HH:MM a job of ours stands for, or "" if it is not one of ours."""
    title = job.get("title", "")
    return title[len(PREFIX):] if title.startswith(PREFIX) else ""


def mine(jobs: list[dict]) -> dict[str, dict]:
    """Our jobs, keyed by the time they fire. Other people's are ignored."""
    return {moment: job for job in jobs if (moment := moment_of(job))}


def build(moment: str, cfg: Config, repo: str, token: str) -> dict:
    """One job: fire this POST, at this one time, in the user's timezone.

    One job per tick rather than one job per slot, because cron-job.org's
    schedule is a cross-product of hours and minutes. Asking a single job for
    09:30 and 10:00 would also buy you 09:00 and 10:30 - harmless, but it makes
    the account impossible to read, and it stops the drift check from ever
    saying a plain yes.
    """
    hour, minute = (int(part) for part in moment.split(":"))

    return {
        "title": title_for(moment),
        "url": dispatch.url(repo),
        "enabled": True,
        "saveResponses": True,          # so a failure can be read back later
        "requestMethod": api.METHOD_POST,
        "extendedData": {
            "headers": {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
            "body": _body(),
        },
        "schedule": {
            "timezone": cfg.timezone,   # the service follows DST for us
            "expiresAt": 0,
            "hours": [hour],
            "minutes": [minute],
            "mdays": api.EVERY,
            "months": api.EVERY,
            "wdays": api.EVERY,
        },
    }


def _body() -> str:
    """The dispatch payload, as the compact JSON string their API expects."""
    return json.dumps(dispatch.body(), separators=(",", ":"))


@dataclass
class Plan:
    """What reconciling would do, or just did."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.deleted)

    def summary(self) -> str:
        if not self.changed:
            return "Already in step."
        parts = [f"{len(self.created)} added" if self.created else "",
                 f"{len(self.updated)} updated" if self.updated else "",
                 f"{len(self.deleted)} removed" if self.deleted else ""]
        return ", ".join(p for p in parts if p) + "."


def stale(job: dict, wanted: dict) -> bool:
    """Does this existing job still say what we would say today?

    Compared against the *listing*, which is deliberate. The listing leaves out
    extendedData - the headers and body - and fetching those per job means one
    API call per job on a check that runs on every page load. That is how the
    app got itself rate limited (429) and stopped being able to check at all.

    So this asks the questions the listing can answer: is it pointed at the
    right repository, is it switched on, and does it fire at the right time.
    Those cover every way a schedule change can leave a job wrong.

    The headers carry the GitHub token, which only changes when you reconnect -
    and reconnecting rewrites every job outright, so nothing has to notice it
    here. cron-job.org's own bookkeeping (last run, next run, failure counts)
    is ignored: a job is not out of date because it has run since we looked.
    """
    # Anything the service did not return cannot be compared. Treating absence
    # as a difference is what made the health check cry drift for ever.
    if job.get("url") and job.get("url") != wanted["url"]:
        return True
    if job.get("requestMethod") is not None and job["requestMethod"] != wanted["requestMethod"]:
        return True
    if job.get("enabled") is False:
        return True

    schedule, want_schedule = job.get("schedule") or {}, wanted["schedule"]
    if schedule.get("timezone") and schedule["timezone"] != want_schedule["timezone"]:
        return True
    for part in ("hours", "minutes"):
        if schedule.get(part) and list(schedule[part]) != want_schedule[part]:
            return True

    return False


def plan(cfg: Config, repo: str, token: str, key: str,
         rewrite: bool = False) -> tuple[Plan, dict]:
    """Work out the difference without changing anything. One API call.

    `rewrite` marks every job we own as needing an update - used after you
    hand over a new GitHub token, because the token lives in the headers and
    the listing never shows those.
    """
    existing = mine(api.list_jobs(key))
    wanted = {moment: build(moment, cfg, repo, token) for moment in trigger_times(cfg)}

    both = sorted(set(wanted) & set(existing))

    return Plan(
        created=sorted(set(wanted) - set(existing)),
        updated=both if rewrite else [m for m in both if stale(existing[m], wanted[m])],
        deleted=sorted(set(existing) - set(wanted)),
    ), existing


def apply(cfg: Config, repo: str, token: str, key: str, rewrite: bool = False) -> Plan:
    """Bring the account in line with the schedule."""
    todo, existing = plan(cfg, repo, token, key, rewrite)

    for moment in todo.created:
        api.create_job(build(moment, cfg, repo, token), key)
    for moment in todo.updated:
        api.update_job(existing[moment]["jobId"], build(moment, cfg, repo, token), key)
    for moment in todo.deleted:
        api.delete_job(existing[moment]["jobId"], key)

    return todo


def drift(cfg: Config, repo: str, token: str, key: str) -> Plan:
    """What is out of step right now - for the health check, which never acts."""
    return plan(cfg, repo, token, key)[0]


def remove_all(key: str) -> int:
    """Delete every job this app created. Used when disconnecting."""
    ours = mine(api.list_jobs(key))
    for job in ours.values():
        api.delete_job(job["jobId"], key)
    return len(ours)
