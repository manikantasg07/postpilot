"""Settings, first run, and the sync badge."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import paths
from agent.config import load_config, save_config
from agent.github.sync import FileStatus
from web.app import app
from web.routers import sync as badge

client = TestClient(app)


# --- first run ---------------------------------------------------------------

@pytest.fixture
def fresh_clone(tmp_path, monkeypatch):
    """No config.json at all - exactly what someone sees after git clone."""
    monkeypatch.setattr(paths, "CONFIG_FILE", tmp_path / "missing.json")


@pytest.mark.parametrize("url", ["/", "/setup", "/setup/linkedin", "/setup/schedule",
                                 "/setup/github", "/drafts", "/topics", "/settings"])
def test_no_page_crashes_on_a_fresh_clone(fresh_clone, url):
    assert client.get(url).status_code == 200


def test_dashboard_welcomes_a_new_user(fresh_clone):
    body = client.get("/").text
    assert "Start setup" in body


# --- voice -------------------------------------------------------------------

def test_settings_page_shows_voice_and_connections():
    body = client.get("/settings").text
    assert "Voice" in body
    assert "Connections" in body


def test_saving_voice_persists_tone_and_context():
    client.post("/settings/voice",
                data={"tone": "storytelling", "author_context": "  Backend engineer.  "},
                follow_redirects=False)
    cfg = load_config()
    assert cfg.tone == "storytelling"
    assert cfg.author_context == "Backend engineer."


def test_unknown_tone_is_refused():
    response = client.post("/settings/voice", data={"tone": "shouty"}, follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert load_config().tone != "shouty"


def test_overlong_context_is_refused():
    response = client.post("/settings/voice",
                           data={"tone": "casual", "author_context": "x" * 700},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]


# --- sync badge --------------------------------------------------------------

def test_badge_is_quiet_when_github_is_not_connected():
    assert client.get("/sync/status").json()["level"] == "off"


def connect(monkeypatch, files, behind=0, ahead=0):
    import dataclasses

    from agent.config import save_config
    save_config(dataclasses.replace(load_config(), github_repo="me/agent"))
    monkeypatch.setattr(badge, "_token", lambda: "gho_test")
    monkeypatch.setattr(badge.sync, "compare", lambda repo, token: files)
    monkeypatch.setattr(badge.sync, "newer_runs", lambda repo, token: behind)
    monkeypatch.setattr(badge.sync, "unpushed_runs", lambda repo, token: ahead)


def test_badge_counts_unpushed_changes(monkeypatch):
    connect(monkeypatch, [FileStatus("data/config.json", "settings", "changed"),
                          FileStatus("data/topics.json", "topics", "same")])
    data = client.get("/sync/status").json()
    assert data["level"] == "push"
    assert data["text"] == "1 change not on GitHub"


def test_badge_says_when_github_is_ahead(monkeypatch):
    connect(monkeypatch, [FileStatus("data/config.json", "settings", "same")], behind=2)
    data = client.get("/sync/status").json()
    assert data["level"] == "pull"


def test_badge_all_green(monkeypatch):
    connect(monkeypatch, [FileStatus("data/config.json", "settings", "same")])
    assert client.get("/sync/status").json()["text"] == "In sync"


def test_badge_answer_is_cached(monkeypatch):
    calls = []
    connect(monkeypatch, [])
    monkeypatch.setattr(badge.sync, "compare", lambda repo, token: calls.append(1) or [])

    client.get("/sync/status")
    client.get("/sync/status")
    assert len(calls) == 1

    client.get("/sync/status?fresh=1")
    assert len(calls) == 2


def test_pull_state_needs_a_connection():
    response = client.post("/sync/pull-state", follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_badge_counts_posts_made_here_that_github_has_not_seen(monkeypatch):
    connect(monkeypatch, [FileStatus("data/config.json", "settings", "same")], ahead=2)
    data = client.get("/sync/status").json()
    assert data["level"] == "push"
    assert data["text"] == "2 changes not on GitHub"


def test_push_state_needs_a_connection():
    response = client.post("/sync/push-state", follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_push_state_reports_how_much_went_up(monkeypatch):
    connect(monkeypatch, [])
    monkeypatch.setattr(badge.sync, "push_state", lambda repo, token: 3)
    response = client.post("/sync/push-state", follow_redirects=False)
    assert "Sent+3+posts" in response.headers["location"]


def test_push_state_says_when_there_was_nothing_to_send(monkeypatch):
    connect(monkeypatch, [])
    monkeypatch.setattr(badge.sync, "push_state", lambda repo, token: 0)
    response = client.post("/sync/push-state", follow_redirects=False)
    assert "already+knew" in response.headers["location"]


def test_pull_state_reports_how_much_came_down(monkeypatch):
    connect(monkeypatch, [])
    monkeypatch.setattr(badge.sync, "pull_state", lambda repo, token: 7)
    response = client.post("/sync/pull-state", follow_redirects=False)
    assert "Pulled+7" in response.headers["location"]


def test_push_can_return_to_settings_but_nowhere_else():
    ok = client.post("/setup/github/push", data={"next": "/settings"}, follow_redirects=False)
    evil = client.post("/setup/github/push", data={"next": "https://evil.example"},
                       follow_redirects=False)
    assert ok.headers["location"].startswith("/settings")
    assert evil.headers["location"].startswith("/setup/github")


# --- writing off a failed slot ----------------------------------------------

def failed_slot(date: str = "2026-09-28", slot: str = "18:30"):
    from agent.state import HistoryEntry, load_state, save_state
    state = load_state()
    state.add(HistoryEntry(date=date, slot=slot, topic="RAG evaluation",
                           status="failed", error="No draft issue was found."))
    save_state(state)
    return state


def test_dismissing_closes_the_slot_without_posting():
    from agent.state import load_state
    failed_slot()

    response = client.post("/settings/dismiss",
                           data={"date": "2026-09-28", "slot": "18:30"},
                           follow_redirects=False)

    assert "done=" in response.headers["location"]
    state = load_state()
    assert state.already_handled("2026-09-28", "18:30")
    assert [h.status for h in state.history if h.date == "2026-09-28"] == ["failed", "skipped"]


def test_dismissing_keeps_the_topic_queued():
    from agent.state import load_state
    before = load_state().next_topic_index
    failed_slot("2026-09-27", "09:00")

    client.post("/settings/dismiss", data={"date": "2026-09-27", "slot": "09:00"},
                follow_redirects=False)

    assert load_state().next_topic_index == before


def test_the_warning_is_gone_afterwards():
    from web import health
    failed_slot("2026-09-26", "09:00")
    assert [i for i in health.issues() if "2026-09-26" in i.title]

    client.post("/settings/dismiss", data={"date": "2026-09-26", "slot": "09:00"},
                follow_redirects=False)

    assert [i for i in health.issues() if "2026-09-26" in i.title] == []


def test_a_slot_that_never_failed_cannot_be_dismissed():
    response = client.post("/settings/dismiss",
                           data={"date": "2026-01-01", "slot": "09:00"},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_dismissing_twice_is_harmless():
    from agent.state import load_state
    failed_slot("2026-09-25", "09:00")
    client.post("/settings/dismiss", data={"date": "2026-09-25", "slot": "09:00"},
                follow_redirects=False)
    client.post("/settings/dismiss", data={"date": "2026-09-25", "slot": "09:00"},
                follow_redirects=False)

    skips = [h for h in load_state().history
             if h.date == "2026-09-25" and h.status == "skipped"]
    assert len(skips) == 1


# --- saving the schedule keeps the outside scheduler in step ----------------

def schedule_form(times=("10:00",)):
    return {"post_times": list(times), "timezone": "America/Los_Angeles", "mode": "preview",
            "preview_minutes": 30, "preview_timeout_action": "post", "catch_up_hours": 6}


def test_saving_the_schedule_pushes_it_to_the_service(monkeypatch):
    """Otherwise cron-job.org keeps firing at yesterday's times."""
    import dataclasses

    from agent.cronjob.sync import Plan
    from web.routers import schedule as page

    save_config(dataclasses.replace(load_config(), github_repo="me/agent"))
    monkeypatch.setattr(page, "get_secret", lambda name: "stored")
    sent = {}
    monkeypatch.setattr(page.cronjob_sync, "apply",
                        lambda cfg, repo, token, key: sent.update(times=cfg.post_times)
                        or Plan(created=["09:30"], deleted=["08:30"]))

    body = client.post("/setup/schedule", data=schedule_form()).text

    assert sent["times"] == ["10:00"]
    assert "1 added, 1 removed" in body       # and it says so, rather than syncing in silence


def test_nothing_is_pushed_when_the_service_is_not_connected(monkeypatch):
    from web.routers import schedule as page
    monkeypatch.setattr(page, "get_secret", lambda name: "")
    monkeypatch.setattr(page.cronjob_sync, "apply",
                        lambda *a, **k: pytest.fail("should not reach the service"))

    assert client.post("/setup/schedule", data=schedule_form()).status_code == 200


def test_a_service_outage_does_not_lose_the_schedule(monkeypatch):
    """The config saved fine. Losing that because a third party was briefly
    unreachable would be worse than being late for one day."""
    import dataclasses

    from agent.cronjob.errors import CronJobError
    from web.routers import schedule as page

    save_config(dataclasses.replace(load_config(), github_repo="me/agent"))
    monkeypatch.setattr(page, "get_secret", lambda name: "stored")

    def boom(*args, **kwargs):
        raise CronJobError("cron-job.org PUT /jobs failed (503)")

    monkeypatch.setattr(page.cronjob_sync, "apply", boom)

    body = client.post("/setup/schedule", data=schedule_form(["11:00"])).text

    assert load_config().post_times == ["11:00"]      # saved anyway
    assert "was not updated" in body and "503" in body


# --- one Save, all four places ----------------------------------------------

def wired(monkeypatch, github=True, service=True):
    """Whatever is connected, connected."""
    import dataclasses

    from web.routers import schedule as page
    save_config(dataclasses.replace(load_config(), github_repo="me/agent"))
    monkeypatch.setattr(page, "get_secret",
                        lambda name: "stored" if (name == "GITHUB_TOKEN") == github
                        or (name != "GITHUB_TOKEN") == service else "")
    return page


def test_saving_pushes_to_github_without_a_second_button(monkeypatch):
    """Actions reads the schedule from the repo. A save that stops at this
    laptop changes nothing at all."""
    import dataclasses

    from web.routers import schedule as page
    save_config(dataclasses.replace(load_config(), github_repo="me/agent"))
    monkeypatch.setattr(page, "get_secret", lambda name: "stored")
    monkeypatch.setattr(page.cronjob_sync, "apply", lambda *a, **k: __import__(
        "agent.cronjob.sync", fromlist=["Plan"]).Plan())

    sent = []
    monkeypatch.setattr(page, "push_settings", lambda repo, token: sent.append(repo)
                        or ["data/config.json", ".github/workflows/post.yml"])

    body = client.post("/setup/schedule", data=schedule_form(["11:30"])).text

    assert sent == ["me/agent"]
    assert "Pushed 2 file(s) to GitHub" in body


def test_a_github_failure_still_saves_and_still_syncs_the_service(monkeypatch):
    """Three independent places. One being unreachable must not stop the others."""
    import dataclasses

    from agent.cronjob.sync import Plan
    from agent.github.errors import GitHubError
    from web.routers import schedule as page

    save_config(dataclasses.replace(load_config(), github_repo="me/agent"))
    monkeypatch.setattr(page, "get_secret", lambda name: "stored")

    def boom(repo, token):
        raise GitHubError("GitHub PUT failed (502)")

    synced = []
    monkeypatch.setattr(page, "push_settings", boom)
    monkeypatch.setattr(page.cronjob_sync, "apply",
                        lambda cfg, repo, token, key: synced.append(cfg.post_times)
                        or Plan(created=["11:00"]))

    body = client.post("/setup/schedule", data=schedule_form(["11:30"])).text

    assert load_config().post_times == ["11:30"]        # saved regardless
    assert synced == [["11:30"]]                        # and the service still got it
    assert "GitHub was not updated" in body and "502" in body


def test_nothing_is_pushed_when_github_is_not_connected(monkeypatch):
    from web.routers import schedule as page
    monkeypatch.setattr(page, "get_secret", lambda name: "")
    monkeypatch.setattr(page, "push_settings",
                        lambda *a, **k: pytest.fail("should not reach GitHub"))

    assert client.post("/setup/schedule", data=schedule_form()).status_code == 200
