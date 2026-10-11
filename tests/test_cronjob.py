"""Owning the outside scheduler's jobs, so the third copy cannot go stale."""
from __future__ import annotations

import dataclasses
import json

import pytest

from agent.config import Config
from agent.cronjob import api, sync
from agent.cronjob.errors import CronJobAuthError, CronJobError

CFG = Config(timezone="America/Los_Angeles", posts_per_day=1, post_times=["10:00"],
             mode="preview", preview_minutes=30, github_repo="me/agent")
REPO, TOKEN, KEY = "me/agent", "github_pat_x", "cjo_key"


def wanted(moment: str, cfg: Config = CFG) -> dict:
    return sync.build(moment, cfg, REPO, TOKEN)


def stored(moment: str, job_id: int = 1, **overrides) -> dict:
    """What the service would hand back for a job we created."""
    job = wanted(moment) | {"jobId": job_id}
    # The API never returns the token, so neither does this.
    job["extendedData"] = dict(job["extendedData"])
    job["extendedData"]["headers"] = dict(job["extendedData"]["headers"])
    job["extendedData"]["headers"]["Authorization"] = "Bearer ***"
    return job | overrides


class FakeService:
    """Enough of cron-job.org to reconcile against."""

    def __init__(self, jobs: list[dict] | None = None):
        self.jobs = {job["jobId"]: job for job in (jobs or [])}
        self.next_id = max(self.jobs, default=0) + 1
        self.calls: list[str] = []

    def install(self, monkeypatch):
        # The real service returns a summary from the listing and the whole
        # job only from /jobs/{id}. Modelling that is the point of this fake.
        monkeypatch.setattr(sync.api, "list_jobs",
                            lambda key: [self._summary(j) for j in self.jobs.values()])
        monkeypatch.setattr(sync.api, "get_job", lambda job_id, key: self.jobs[job_id])
        monkeypatch.setattr(sync.api, "create_job", self._create)
        monkeypatch.setattr(sync.api, "update_job", self._update)
        monkeypatch.setattr(sync.api, "delete_job", self._delete)
        return self

    @staticmethod
    def _summary(job: dict) -> dict:
        """What GET /jobs gives back: no extendedData."""
        return {k: v for k, v in job.items() if k != "extendedData"}

    def _create(self, job, key):
        self.calls.append(f"create {job['title']}")
        job_id, self.next_id = self.next_id, self.next_id + 1
        self.jobs[job_id] = job | {"jobId": job_id}
        return job_id

    def _update(self, job_id, changes, key):
        self.calls.append(f"update {job_id}")
        self.jobs[job_id] = self.jobs[job_id] | changes

    def _delete(self, job_id, key):
        self.calls.append(f"delete {job_id}")
        self.jobs.pop(job_id)

    def titles(self) -> list[str]:
        return sorted(job["title"] for job in self.jobs.values())


# --- what one job says -------------------------------------------------------

def test_a_job_fires_at_exactly_one_time():
    """Their schedule is a cross-product, so one job per tick keeps it exact."""
    job = wanted("09:30")
    assert job["schedule"]["hours"] == [9]
    assert job["schedule"]["minutes"] == [30]
    assert job["schedule"]["mdays"] == api.EVERY


def test_the_job_carries_the_users_timezone_not_utc():
    """The service handles daylight saving; post.yml's cron cannot."""
    assert wanted("09:30")["schedule"]["timezone"] == "America/Los_Angeles"


def test_the_job_posts_the_dispatch_payload():
    job = wanted("09:30")
    assert job["requestMethod"] == api.METHOD_POST
    assert job["url"].endswith("/actions/workflows/post.yml/dispatches")
    assert json.loads(job["extendedData"]["body"])["inputs"]["mode"] == "due"


def test_the_job_carries_the_narrow_token():
    assert wanted("09:30")["extendedData"]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_our_jobs_are_named_so_they_can_be_found_again():
    assert sync.moment_of(wanted("09:30")) == "09:30"


@pytest.mark.parametrize("title", ["PostCadence backup", "postcadence 09:30", "Something else"])
def test_somebody_elses_job_is_never_mistaken_for_ours(title):
    assert sync.moment_of({"title": title}) == ""


# --- reconciling -------------------------------------------------------------

def test_an_empty_account_gets_every_tick(monkeypatch):
    service = FakeService().install(monkeypatch)

    plan = sync.apply(CFG, REPO, TOKEN, KEY)

    assert plan.created == ["09:30", "10:00", "10:30"]
    assert service.titles() == ["PostCadence · 09:30", "PostCadence · 10:00",
                                "PostCadence · 10:30"]


def test_running_it_twice_changes_nothing(monkeypatch):
    """The property that lets the app call this on every save."""
    service = FakeService().install(monkeypatch)
    sync.apply(CFG, REPO, TOKEN, KEY)
    service.calls.clear()

    plan = sync.apply(CFG, REPO, TOKEN, KEY)

    assert not plan.changed
    assert service.calls == []
    assert plan.summary() == "Already in step."


def test_moving_the_posting_time_moves_the_jobs(monkeypatch):
    service = FakeService().install(monkeypatch)
    sync.apply(CFG, REPO, TOKEN, KEY)

    evening = dataclasses.replace(CFG, post_times=["18:30"])
    plan = sync.apply(evening, REPO, TOKEN, KEY)

    assert plan.created == ["18:00", "18:30", "19:00"]
    assert plan.deleted == ["09:30", "10:00", "10:30"]
    assert service.titles() == ["PostCadence · 18:00", "PostCadence · 18:30",
                                "PostCadence · 19:00"]


def test_a_second_slot_only_adds(monkeypatch):
    FakeService().install(monkeypatch)
    sync.apply(CFG, REPO, TOKEN, KEY)

    two = dataclasses.replace(CFG, posts_per_day=2, post_times=["10:00", "18:30"])
    plan = sync.apply(two, REPO, TOKEN, KEY)

    assert plan.created == ["18:00", "18:30", "19:00"]
    assert plan.deleted == []


def test_a_half_finished_run_is_finished_next_time(monkeypatch):
    """One tick already there, two missing - it fills the gap, not a duplicate."""
    service = FakeService([stored("10:00", job_id=7)]).install(monkeypatch)

    plan = sync.apply(CFG, REPO, TOKEN, KEY)

    assert plan.created == ["09:30", "10:30"]
    assert plan.updated == []
    assert len(service.jobs) == 3


def test_other_peoples_jobs_are_left_alone(monkeypatch):
    theirs = {"jobId": 99, "title": "My backup ping", "url": "https://example.com"}
    service = FakeService([theirs]).install(monkeypatch)

    sync.apply(CFG, REPO, TOKEN, KEY)

    assert 99 in service.jobs
    assert "delete 99" not in service.calls


# --- noticing that an existing job no longer matches -------------------------

def test_a_changed_timezone_is_an_update_not_a_delete(monkeypatch):
    service = FakeService([stored("09:30", job_id=3) | {
        "schedule": wanted("09:30")["schedule"] | {"timezone": "Asia/Kolkata"}}]
    ).install(monkeypatch)

    plan = sync.apply(CFG, REPO, TOKEN, KEY)

    assert "09:30" in plan.updated
    assert plan.deleted == []
    assert service.jobs[3]["schedule"]["timezone"] == "America/Los_Angeles"


def test_a_disabled_job_is_switched_back_on(monkeypatch):
    FakeService([stored("09:30", job_id=3, enabled=False)]).install(monkeypatch)
    assert "09:30" in sync.apply(CFG, REPO, TOKEN, KEY).updated


def test_a_job_pointing_at_the_wrong_repo_is_fixed(monkeypatch):
    FakeService([stored("09:30", job_id=3, url="https://api.github.com/repos/old/repo"
                                                "/actions/workflows/post.yml/dispatches")]
                ).install(monkeypatch)
    assert "09:30" in sync.apply(CFG, REPO, TOKEN, KEY).updated


def test_a_masked_token_is_not_read_as_a_change(monkeypatch):
    """The API never hands the token back. Comparing it would mean rewriting
    every job on every save, for ever."""
    service = FakeService([stored(m, job_id=i) for i, m
                           in enumerate(["09:30", "10:00", "10:30"], start=1)]
                          ).install(monkeypatch)

    assert not sync.apply(CFG, REPO, TOKEN, KEY).changed
    assert service.calls == []


def test_headers_are_not_compared_on_the_routine_check(monkeypatch):
    """Deliberate. The listing omits them, and fetching them per job means one
    API call per job on a check that runs on every page load - which is how the
    app earned a 429 and stopped being able to check anything at all."""
    bare = stored("09:30", job_id=3)
    bare["extendedData"] = {"headers": {}, "body": ""}
    FakeService([bare]).install(monkeypatch)

    assert "09:30" not in sync.apply(CFG, REPO, TOKEN, KEY).updated


def test_a_new_token_rewrites_every_job(monkeypatch):
    """The token lives in the headers, which nothing compares. Reconnecting is
    the moment it can have changed, so that rewrites all of them outright."""
    FakeService([stored(m, job_id=i) for i, m
                 in enumerate(["09:30", "10:00", "10:30"], start=1)]).install(monkeypatch)

    plan = sync.apply(CFG, REPO, "a_new_token", KEY, rewrite=True)

    assert plan.updated == ["09:30", "10:00", "10:30"]




# --- the health check's read-only view ---------------------------------------

def test_drift_reports_without_touching_anything(monkeypatch):
    service = FakeService().install(monkeypatch)

    plan = sync.drift(CFG, REPO, TOKEN, KEY)

    assert plan.created == ["09:30", "10:00", "10:30"]
    assert service.calls == []          # a health check must never act


def test_drift_is_quiet_when_everything_matches(monkeypatch):
    FakeService([stored(m, job_id=i) for i, m
                 in enumerate(["09:30", "10:00", "10:30"], start=1)]).install(monkeypatch)
    assert not sync.drift(CFG, REPO, TOKEN, KEY).changed


# --- disconnecting -----------------------------------------------------------

def test_disconnecting_removes_only_our_jobs(monkeypatch):
    theirs = {"jobId": 99, "title": "My backup ping"}
    service = FakeService([stored("09:30", job_id=1), theirs]).install(monkeypatch)

    assert sync.remove_all(KEY) == 1
    assert list(service.jobs) == [99]


# --- the API wrapper ---------------------------------------------------------

class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):
        return self._payload


def test_a_bad_key_says_so_in_plain_words(monkeypatch):
    monkeypatch.setattr(api.requests, "request", lambda *a, **k: FakeResponse(401))
    with pytest.raises(CronJobAuthError, match="refused the API key"):
        api.list_jobs("wrong")


def test_another_failure_keeps_the_status_code(monkeypatch):
    monkeypatch.setattr(api.requests, "request",
                        lambda *a, **k: FakeResponse(500, text="boom"))
    with pytest.raises(CronJobError, match="500"):
        api.list_jobs(KEY)


def test_the_key_travels_as_a_bearer_token(monkeypatch):
    seen = {}

    def capture(method, url, headers, timeout, **kw):
        seen.update(method=method, url=url, headers=headers)
        return FakeResponse(200, {"jobs": []})

    monkeypatch.setattr(api.requests, "request", capture)
    api.list_jobs(KEY)

    assert seen["headers"]["Authorization"] == f"Bearer {KEY}"
    assert seen["url"] == "https://api.cron-job.org/jobs"


def test_deleting_tolerates_an_empty_response(monkeypatch):
    monkeypatch.setattr(api.requests, "request", lambda *a, **k: FakeResponse(200))
    assert api.delete_job(1, KEY) is None



# --- the drift that would never clear ---------------------------------------

def test_a_summary_listing_is_not_read_as_drift(monkeypatch):
    """The regression that made the dashboard say "6 updated" for ever.

    GET /jobs omits extendedData. Comparing against that made every header
    look missing, so every check reported every job as needing an update, and
    pressing Sync changed nothing because nothing was actually wrong.
    """
    jobs = [stored(m, job_id=i) for i, m in enumerate(["09:30", "10:00", "10:30"], start=1)]
    service = FakeService(jobs).install(monkeypatch)
    # No detail endpoint at all: the summary is all we get.
    monkeypatch.setattr(sync.api, "get_job",
                        lambda job_id, key: FakeService._summary(service.jobs[job_id]))

    assert not sync.drift(CFG, REPO, TOKEN, KEY).changed


def test_the_routine_check_makes_one_api_call(monkeypatch):
    """Seven calls per page load is what got us rate limited."""
    service = FakeService([stored(m, job_id=i) for i, m
                           in enumerate(["09:30", "10:00", "10:30"], start=1)]).install(monkeypatch)
    calls = []
    monkeypatch.setattr(sync.api, "list_jobs",
                        lambda key: calls.append("list") or [FakeService._summary(j)
                                                             for j in service.jobs.values()])
    monkeypatch.setattr(sync.api, "get_job",
                        lambda job_id, key: pytest.fail("the drift check must not fetch details"))

    sync.drift(CFG, REPO, TOKEN, KEY)

    assert calls == ["list"]



def test_rate_limiting_is_its_own_kind_of_error(monkeypatch):
    """429 means "you asked too often", not "your cronjobs are wrong". The
    difference matters: one is a reason to wait, the other to act."""
    from agent.cronjob.errors import CronJobBusyError
    monkeypatch.setattr(api.requests, "request", lambda *a, **k: FakeResponse(429, text="slow down"))

    with pytest.raises(CronJobBusyError, match="rate limiting"):
        api.list_jobs(KEY)



def test_a_real_difference_is_still_caught_through_the_detail(monkeypatch):
    wrong = stored("09:30", job_id=1)
    wrong["schedule"] = wanted("09:30")["schedule"] | {"timezone": "Asia/Kolkata"}
    FakeService([wrong]).install(monkeypatch)

    assert "09:30" in sync.apply(CFG, REPO, TOKEN, KEY).updated


def test_a_moved_tick_is_caught_even_if_the_title_is_right(monkeypatch):
    """A job titled 09:30 that actually fires at 08:00 is worse than a missing one."""
    drifted = stored("09:30", job_id=1)
    drifted["schedule"] = wanted("09:30")["schedule"] | {"hours": [8], "minutes": [0]}
    FakeService([drifted]).install(monkeypatch)

    assert "09:30" in sync.apply(CFG, REPO, TOKEN, KEY).updated
