"""Everything that could stop tomorrow's post, found before tomorrow.

Each check is a plain question about local files and the keyring - no network
- so the dashboard can ask all of them on every page load.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent.config import Config, is_first_run, load_or_default
from agent.cronjob import sync as cronjob_sync
from agent.cronjob.errors import CronJobBusyError, CronJobError
from agent.llm import KEY_NAMES
from agent.schedule import cron_lines
from agent.secrets_store import get_secret
from agent.state import load_state
from agent.status import token_days
from agent.topics import load_topics
from web.workflow import current_cron, workflow_path

EXPIRY_WARNING_DAYS = 7
LOW_TOPICS = 3
ORDER = {"bad": 0, "warn": 1, "info": 2}


@dataclass
class Issue:
    level: str     # bad = posting will fail, warn = will fail soon, info = worth doing
    title: str
    detail: str
    href: str
    action: str
    # A slot that failed can be written off instead of acted on. These carry the
    # date and slot to close; everything else is a problem you have to fix, and
    # dismissing it would only hide it until the next page load.
    dismiss_date: str = ""
    dismiss_slot: str = ""

    @property
    def dismissable(self) -> bool:
        return bool(self.dismiss_date and self.dismiss_slot)


def _secret_value(name: str) -> str:
    try:
        return get_secret(name) or ""
    except Exception:
        return ""


def _secret(name: str | None) -> bool:
    try:
        return bool(name and get_secret(name))
    except Exception:          # keyring missing or locked counts as "not there"
        return False


def check_model(cfg: Config) -> Issue | None:
    key = KEY_NAMES.get(cfg.llm_provider)
    if key and not _secret(key):
        return Issue("bad", "No API key for the writer",
                     f"{cfg.llm_provider} needs {key} before it can write anything.",
                     "/setup", "Add key")
    return None


def check_linkedin(cfg: Config) -> Issue | None:
    if not (cfg.linkedin_member_id and _secret("LINKEDIN_ACCESS_TOKEN")):
        return Issue("bad", "LinkedIn is not connected",
                     "Posts are written but have nowhere to go.",
                     "/setup/linkedin", "Connect")

    days = token_days(cfg)
    if days is None:
        return None
    if days < 0:
        return Issue("bad", "LinkedIn token has expired",
                     "LinkedIn tokens last 60 days. Sign in again to keep posting.",
                     "/setup/linkedin/start", "Reconnect")
    if days <= EXPIRY_WARNING_DAYS:
        return Issue("warn", f"LinkedIn token expires in {days} day{'s' if days != 1 else ''}",
                     "Reconnect now and the scheduler never notices.",
                     "/setup/linkedin/start", "Reconnect")
    return None


def check_schedule(cfg: Config) -> Issue | None:
    path = workflow_path()
    if not path.exists():
        return None
    if set(current_cron(path.read_text(encoding="utf-8"))) != set(cron_lines(cfg)):
        return Issue("warn", "Schedule and workflow disagree",
                     "post.yml would wake up at different times from the ones you picked.",
                     "/setup/schedule", "Fix schedule")
    return None


def check_topics(cfg: Config) -> Issue | None:
    topics = load_topics()
    if not topics:
        return Issue("bad", "No topics yet",
                     "The writer needs at least one idea to write about.",
                     "/topics", "Add topics")

    left = len(topics) - load_state().next_topic_index
    if left <= 0:
        return Issue("warn", "Topic list used up",
                     "New posts will revisit yesterday's topic from a fresh angle.",
                     "/topics", "Add topics")
    if left <= LOW_TOPICS:
        return Issue("info", f"Only {left} new topic{'s' if left != 1 else ''} left",
                     "After that the agent starts repeating topics.",
                     "/topics", "Add topics")
    return None


def check_last_run(cfg: Config) -> Issue | None:
    """A failure nobody looked at is the same as no warning at all."""
    state = load_state()
    failures = [h for h in state.history if h.status == "failed"]
    if not failures:
        return None

    last = failures[-1]
    if state.already_handled(last.date, last.slot):
        return None                        # something later covered that slot

    return Issue("warn", f"{last.date} {last.slot} did not post",
                 last.error or "The run failed and nothing was published.",
                 "/drafts", "Write one now",
                 dismiss_date=last.date, dismiss_slot=last.slot)


# Every page load asks every check. This one talks to a third party over the
# network, so its answer is kept for a while: without that, opening the
# dashboard twice in a minute is two more requests cron-job.org did not need,
# and enough of them earns a 429 that makes the check useless.
DRIFT_TTL = timedelta(minutes=10)
_drift_cache: dict[str, object] = {}


def forget_trigger_cache() -> None:
    """Called after a sync, so the page shows the result and not the memory."""
    _drift_cache.clear()


def _drift(cfg: Config, repo: str, token: str, key: str):
    fresh = _drift_cache.get("at")
    if isinstance(fresh, datetime) and datetime.now(timezone.utc) - fresh < DRIFT_TTL:
        return _drift_cache["plan"]

    plan = cronjob_sync.drift(cfg, repo, token, key)
    _drift_cache.update(at=datetime.now(timezone.utc), plan=plan)
    return plan


def check_trigger(cfg: Config) -> Issue | None:
    """Is the outside scheduler still firing at the times we post at?

    Only asked when the app has been given the keys to manage it. Somebody who
    set their cronjobs up by hand is not nagged about a thing this check
    cannot see.
    """
    key = _secret_value("CRONJOB_API_KEY")
    token = _secret_value("GITHUB_DISPATCH_TOKEN")
    if not (key and token and cfg.github_repo):
        return None

    try:
        plan = _drift(cfg, cfg.github_repo, token, key)
    except CronJobBusyError:
        # Not a problem with the schedule - we asked too often. Saying nothing
        # is better than an alarm about something that is probably fine.
        return None
    except (CronJobError, OSError) as exc:
        return Issue("info", "Could not check cron-job.org",
                     f"{exc} Your schedule is probably fine; this is only the check failing.",
                     "/setup/trigger", "Open")

    if not plan.changed:
        return None

    return Issue("warn", "Your punctual schedule is out of date",
                 f"cron-job.org fires at different times from the ones you picked ({plan.summary()})",
                 "/setup/trigger", "Sync now")


def check_github(cfg: Config) -> Issue | None:
    if not (cfg.github_repo and _secret("GITHUB_TOKEN")):
        return Issue("info", "GitHub is not connected here",
                     "Connect it to push settings and keys from this app.",
                     "/setup/github", "Connect")
    return None


CHECKS = (check_model, check_linkedin, check_schedule, check_topics,
          check_last_run, check_trigger, check_github)


def issues(cfg: Config | None = None) -> list[Issue]:
    """Every problem, worst first. An empty list means all clear."""
    if is_first_run():
        return [Issue("bad", "Welcome - nothing is set up yet",
                      "Five short steps and the agent can post on its own.",
                      "/setup", "Start setup")]

    cfg = cfg or load_or_default()
    found = [issue for check in CHECKS if (issue := check(cfg))]
    return sorted(found, key=lambda issue: ORDER[issue.level])
