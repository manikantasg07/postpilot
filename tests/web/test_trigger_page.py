"""The punctuality page: everything you paste elsewhere, computed not typed."""
from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from agent.config import load_config, save_config
from web.app import app
from web.routers import trigger

client = TestClient(app)


@pytest.fixture
def connected(monkeypatch):
    save_config(dataclasses.replace(load_config(), github_repo="me/agent"))
    monkeypatch.setattr(trigger, "_token", lambda: "ghp_test")


def test_the_page_opens_without_github(monkeypatch):
    monkeypatch.setattr(trigger, "_token", lambda: "")
    assert client.get("/setup/trigger").status_code == 200


def test_it_shows_a_placeholder_until_the_repo_is_known(monkeypatch):
    monkeypatch.setattr(trigger, "_token", lambda: "")
    save_config(dataclasses.replace(load_config(), github_repo=""))
    assert "YOUR-NAME/YOUR-REPO" in client.get("/setup/trigger").text


def test_it_fills_in_the_real_repo(connected):
    body = client.get("/setup/trigger").text
    assert "/repos/me/agent/actions/workflows/post.yml/dispatches" in body


def test_it_prints_the_times_to_enter(connected):
    save_config(dataclasses.replace(load_config(), posts_per_day=1, post_times=["10:00"],
                                    mode="preview", preview_minutes=30))
    body = client.get("/setup/trigger").text
    for moment in ("09:30", "10:00", "10:30"):
        assert moment in body


def test_it_names_the_timezone_the_service_must_use(connected):
    save_config(dataclasses.replace(load_config(), timezone="Asia/Kolkata"))
    assert "Asia/Kolkata" in client.get("/setup/trigger").text


def test_it_asks_for_the_narrow_token_scope(connected):
    body = client.get("/setup/trigger").text
    assert "Actions: Read and write" in body
    assert "Only select repositories" in body


def test_the_test_button_needs_a_connection(monkeypatch):
    monkeypatch.setattr(trigger, "_token", lambda: "")
    response = client.post("/setup/trigger/test", follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_the_test_button_sends_a_dry_run(connected, monkeypatch):
    """Proving the wiring must never cost a real post."""
    sent = {}
    monkeypatch.setattr(trigger.dispatch, "run_now",
                        lambda repo, token, dry_run=False: sent.update(repo=repo, dry_run=dry_run))

    response = client.post("/setup/trigger/test", follow_redirects=False)

    assert sent == {"repo": "me/agent", "dry_run": True}
    assert "done=" in response.headers["location"]


def test_a_refused_token_is_explained_not_swallowed(connected, monkeypatch):
    from agent.github.errors import GitHubAuthError

    def refuse(*args, **kwargs):
        raise GitHubAuthError("GitHub refused the token (403).")

    monkeypatch.setattr(trigger.dispatch, "run_now", refuse)
    response = client.post("/setup/trigger/test", follow_redirects=False)
    assert "error=" in response.headers["location"]
    assert "403" in response.headers["location"]


def test_the_schedule_page_points_here():
    assert "/setup/trigger" in client.get("/setup/schedule").text


def test_settings_points_here():
    assert "/setup/trigger" in client.get("/settings").text


# --- letting the app own the cronjobs ---------------------------------------

@pytest.fixture
def automatic(connected, monkeypatch):
    """Both keys stored, and a service that answers."""
    store = {"CRONJOB_API_KEY": "cjo_key", "GITHUB_DISPATCH_TOKEN": "github_pat_x"}
    monkeypatch.setattr(trigger, "_secret", lambda name: store.get(name, "gho_test"))
    return store


def test_the_offer_appears_before_you_connect(monkeypatch):
    monkeypatch.setattr(trigger, "_secret", lambda name: "")
    body = client.get("/setup/trigger").text
    assert "Let the app manage it for you" in body
    assert "cron-job.org API key" in body


def test_it_warns_which_token_belongs_in_someone_elses_database(monkeypatch):
    monkeypatch.setattr(trigger, "_secret", lambda name: "")
    body = client.get("/setup/trigger").text
    assert "Actions: Read and write" in body
    assert "that one can do far more" in body      # the warning about reusing the sign-in token


def test_connected_shows_the_managed_times(automatic, monkeypatch):
    monkeypatch.setattr(trigger.sync.api, "list_jobs",
                        lambda key: [{"title": "PostCadence · 09:30"},
                                     {"title": "PostCadence · 10:00"},
                                     {"title": "Someone else's job"}])
    body = client.get("/setup/trigger").text
    assert "Connected" in body
    assert "09:30" in body and "10:00" in body
    assert "Someone else" not in body       # other people's jobs are not ours to show


def test_a_service_outage_does_not_break_the_page(automatic, monkeypatch):
    from agent.cronjob.errors import CronJobError

    def boom(key):
        raise CronJobError("cron-job.org GET /jobs failed (503)")

    monkeypatch.setattr(trigger.sync.api, "list_jobs", boom)
    response = client.get("/setup/trigger")
    assert response.status_code == 200
    assert "503" in response.text


def test_connecting_needs_a_repo(monkeypatch):
    import dataclasses
    monkeypatch.setattr(trigger, "_secret", lambda name: "")
    save_config(dataclasses.replace(load_config(), github_repo=""))
    response = client.post("/setup/trigger/connect",
                           data={"api_key": "k", "dispatch_token": "t"},
                           follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_connecting_stores_both_and_proves_the_key(automatic, monkeypatch):
    saved = {}
    monkeypatch.setattr(trigger, "set_secret", lambda name, value: saved.update({name: value}))
    monkeypatch.setattr(trigger.sync.api, "whoami", lambda key: 3)

    response = client.post("/setup/trigger/connect",
                           data={"api_key": "cjo_new", "dispatch_token": "github_pat_new"},
                           follow_redirects=False)

    assert saved == {"CRONJOB_API_KEY": "cjo_new", "GITHUB_DISPATCH_TOKEN": "github_pat_new"}
    assert "Connected" in response.headers["location"]


def test_a_blank_field_keeps_what_was_there(automatic, monkeypatch):
    """So you can change one key without retyping the other."""
    saved = {}
    monkeypatch.setattr(trigger, "set_secret", lambda name, value: saved.update({name: value}))
    monkeypatch.setattr(trigger.sync.api, "whoami", lambda key: 3)

    client.post("/setup/trigger/connect", data={"api_key": "only_this", "dispatch_token": "  "},
                follow_redirects=False)

    assert list(saved) == ["CRONJOB_API_KEY"]


def test_syncing_reports_what_changed(automatic, monkeypatch):
    from agent.cronjob.sync import Plan
    seen = {}
    monkeypatch.setattr(trigger.sync, "apply",
                        lambda cfg, repo, token, key, rewrite=False: seen.update(rewrite=rewrite)
                        or Plan(created=["09:30"], deleted=["08:30"]))

    response = client.post("/setup/trigger/sync", follow_redirects=False)

    # Sync pushes the token out again: nothing else ever compares the headers.
    assert seen["rewrite"] is True

    assert "1+added" in response.headers["location"]
    assert "1+removed" in response.headers["location"]


def test_syncing_needs_a_connection(monkeypatch):
    monkeypatch.setattr(trigger, "_secret", lambda name: "")
    response = client.post("/setup/trigger/sync", follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_disconnecting_removes_the_jobs_before_forgetting_the_key(automatic, monkeypatch):
    order = []
    monkeypatch.setattr(trigger.sync, "remove_all", lambda key: order.append("removed") or 3)
    monkeypatch.setattr(trigger, "delete_secret", lambda name: order.append(f"forgot {name}"))

    response = client.post("/setup/trigger/disconnect", follow_redirects=False)

    assert order[0] == "removed"        # keys first would strand the jobs
    assert "removed+3" in response.headers["location"]


def test_a_failed_removal_keeps_the_keys(automatic, monkeypatch):
    """Forgetting the key after a failed delete would leave jobs nothing can reach."""
    from agent.cronjob.errors import CronJobError

    def boom(key):
        raise CronJobError("503")

    forgotten = []
    monkeypatch.setattr(trigger.sync, "remove_all", boom)
    monkeypatch.setattr(trigger, "delete_secret", lambda name: forgotten.append(name))

    response = client.post("/setup/trigger/disconnect", follow_redirects=False)

    assert forgotten == []
    assert "error=" in response.headers["location"]
