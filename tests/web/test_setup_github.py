"""The GitHub page: saving the app details, signing in, pushing, sending keys."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent.config import load_config
from agent.github import device
from agent.github.errors import GitHubAuthError
from web.app import app
from web.routers import github as page

client = TestClient(app)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """No token and no network unless a test asks for them."""
    page._pending.clear()
    monkeypatch.setattr(page, "get_secret", lambda name: "")
    monkeypatch.setattr(page.api, "repo_from_remote", lambda *a, **k: "")


def connected(monkeypatch, secrets=("OPENAI_API_KEY",)):
    monkeypatch.setattr(page, "get_secret",
                        lambda name: "gho_test" if name == "GITHUB_TOKEN"
                        else ("value" if name in secrets else ""))
    monkeypatch.setattr(page.api, "current_user", lambda token: "manikanta")
    monkeypatch.setattr(page.api, "recent_runs", lambda *a, **k: [])
    monkeypatch.setattr(page.repo_secrets, "existing_secrets", lambda *a, **k: [])


def test_page_asks_for_the_client_id_first():
    body = client.get("/setup/github").text
    assert "Enable Device Flow" in body
    assert "Connect GitHub" in body


def test_saving_the_client_id_and_repo_persists_them():
    client.post("/setup/github/client",
                data={"client_id": "Iv1.abc", "repo": "me/agent"}, follow_redirects=False)
    cfg = load_config()
    assert cfg.github_client_id == "Iv1.abc"
    assert cfg.github_repo == "me/agent"


def test_the_detected_remote_is_offered(monkeypatch):
    monkeypatch.setattr(page.api, "repo_from_remote", lambda *a, **k: "me/from-remote")
    assert "me/from-remote" in client.get("/setup/github").text


def test_connect_without_a_client_id_is_refused():
    response = client.post("/setup/github/start")
    assert response.status_code == 400
    assert "error" in response.json()


def test_start_hands_the_page_the_short_code(monkeypatch):
    client.post("/setup/github/client", data={"client_id": "Iv1.abc", "repo": "me/agent"},
                follow_redirects=False)
    monkeypatch.setattr(page.device, "start", lambda client_id: {
        "device_code": "long", "user_code": "ABCD-1234",
        "verification_uri": "https://github.com/login/device", "interval": 5, "expires_in": 900})

    body = client.post("/setup/github/start").json()
    assert body["user_code"] == "ABCD-1234"
    assert page._pending["device_code"] == "long"        # the long one never reaches the page


def test_polling_reports_waiting_until_you_approve(monkeypatch):
    page._pending["device_code"] = "long"
    monkeypatch.setattr(page.device, "poll_once", lambda *a: device.Answer(reason="authorization_pending"))
    assert client.post("/setup/github/poll").json() == {
        "status": "waiting", "reason": "authorization_pending", "slow_down": False}


def test_being_told_to_slow_down_is_not_the_same_as_waiting(monkeypatch):
    """GitHub's rule is to poll five seconds slower after this. Treat the two
    alike and it can answer slow_down for ever, which looks exactly like the
    user never approving."""
    page._pending["device_code"] = "long"
    monkeypatch.setattr(page.device, "poll_once", lambda *a: device.Answer(reason="slow_down"))

    body = client.post("/setup/github/poll").json()

    assert body["slow_down"] is True
    assert body["status"] == "waiting"


def test_approval_stores_the_token(monkeypatch):
    page._pending["device_code"] = "long"
    saved = {}
    monkeypatch.setattr(page.device, "poll_once", lambda *a: device.Answer(token="gho_new"))
    monkeypatch.setattr(page, "set_secret", lambda name, value: saved.update({name: value}))
    monkeypatch.setattr(page.api, "current_user", lambda token: "manikanta")

    body = client.post("/setup/github/poll").json()
    assert body == {"status": "connected", "login": "manikanta"}
    assert saved == {"GITHUB_TOKEN": "gho_new"}
    assert page._pending == {}


def test_a_refusal_clears_the_pending_sign_in(monkeypatch):
    page._pending["device_code"] = "long"

    def denied(*args):
        raise GitHubAuthError("You cancelled the approval on GitHub.")

    monkeypatch.setattr(page.device, "poll_once", denied)
    assert client.post("/setup/github/poll").status_code == 400
    assert page._pending == {}


def test_pushing_without_a_connection_is_refused():
    response = client.post("/setup/github/push", follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_push_only_sends_files_that_differ(monkeypatch):
    connected(monkeypatch)
    client.post("/setup/github/client", data={"client_id": "Iv1.abc", "repo": "me/agent"},
                follow_redirects=False)

    sent = {}
    monkeypatch.setattr(page, "_repo_file", lambda path: "same" if path.endswith("topics.json") else "new")
    monkeypatch.setattr(page.api, "get_file", lambda repo, path, token: ("same", "sha"))
    monkeypatch.setattr(page.api, "put_files",
                        lambda repo, files, message, token: sent.update(files) or "sha")

    client.post("/setup/github/push", follow_redirects=False)
    assert "data/topics.json" not in sent
    assert "data/config.json" in sent


def test_the_settings_travel_as_one_commit(monkeypatch):
    """config.json and post.yml describe the same schedule. Committing them
    separately leaves a commit where the repository contradicts itself - and
    that commit fails the cron-drift test, so CI goes red and the phone buzzes
    about a change that was perfectly correct."""
    connected(monkeypatch)
    client.post("/setup/github/client", data={"client_id": "Iv1.abc", "repo": "me/agent"},
                follow_redirects=False)

    commits = []
    monkeypatch.setattr(page, "_repo_file", lambda path: "new " + path)
    monkeypatch.setattr(page.api, "get_file", lambda repo, path, token: ("old", "sha"))
    monkeypatch.setattr(page.api, "put_files",
                        lambda repo, files, message, token: commits.append(sorted(files)) or "sha")

    client.post("/setup/github/push", follow_redirects=False)

    assert len(commits) == 1
    assert "data/config.json" in commits[0]
    assert ".github/workflows/post.yml" in commits[0]


def test_secrets_are_sealed_once_per_push(monkeypatch):
    connected(monkeypatch, secrets=("OPENAI_API_KEY", "LINKEDIN_ACCESS_TOKEN"))
    client.post("/setup/github/client", data={"client_id": "Iv1.abc", "repo": "me/agent"},
                follow_redirects=False)

    keys_fetched, pushed = [], []
    monkeypatch.setattr(page.repo_secrets, "public_key",
                        lambda repo, token: keys_fetched.append(repo) or ("key", "id"))
    monkeypatch.setattr(page.repo_secrets, "put_secret",
                        lambda repo, name, value, token, key, key_id: pushed.append(name))

    response = client.post("/setup/github/secrets", follow_redirects=False)
    assert sorted(pushed) == ["LINKEDIN_ACCESS_TOKEN", "OPENAI_API_KEY"]
    assert len(keys_fetched) == 1          # one key fetch, not one per secret
    assert "done=" in response.headers["location"]


def test_nothing_to_send_says_so(monkeypatch):
    connected(monkeypatch, secrets=())
    client.post("/setup/github/client", data={"client_id": "Iv1.abc", "repo": "me/agent"},
                follow_redirects=False)
    response = client.post("/setup/github/secrets", follow_redirects=False)
    assert "error=" in response.headers["location"]


def test_state_json_is_never_pushed():
    assert all(path != "data/state.json" for path, _ in page.SYNCED)


def test_a_blank_field_keeps_what_was_saved_before():
    """Correcting the repo must not wipe the Client ID: they share one form."""
    client.post("/setup/github/client",
                data={"client_id": "Iv1.abc", "repo": "me/agent"}, follow_redirects=False)
    client.post("/setup/github/client",
                data={"client_id": "", "repo": "me/renamed"}, follow_redirects=False)

    cfg = load_config()
    assert cfg.github_client_id == "Iv1.abc"
    assert cfg.github_repo == "me/renamed"


# --- the sign-in must never fail in silence ---------------------------------

def test_a_keyring_that_refuses_is_reported_not_swallowed(monkeypatch):
    """GitHub approved, but the token could not be stored.

    The page polls every five seconds and only stops when it is told to. A 500
    here returns HTML, the browser cannot parse it, and the flow sits on
    "Waiting for you to approve..." for ever - after the approval has already
    happened.
    """
    from web.routers import github as page

    monkeypatch.setitem(page._pending, "device_code", "dev_code")
    monkeypatch.setattr(page.device, "poll_once", lambda client_id, code: device.Answer(token="gho_new"))
    monkeypatch.setattr(page, "set_secret",
                        lambda name, value: (_ for _ in ()).throw(RuntimeError("keyring locked")))

    response = client.post("/setup/github/poll")

    assert response.status_code == 400
    assert "could not be saved" in response.json()["error"]
    assert "keyring locked" in response.json()["error"]


def test_a_token_that_github_will_not_explain_is_reported(monkeypatch):
    from agent.github.errors import GitHubError
    from web.routers import github as page

    def refuse(token):
        raise GitHubError("GitHub GET /user failed (403)")

    monkeypatch.setitem(page._pending, "device_code", "dev_code")
    monkeypatch.setattr(page.device, "poll_once", lambda client_id, code: device.Answer(token="gho_new"))
    monkeypatch.setattr(page, "set_secret", lambda name, value: None)
    monkeypatch.setattr(page.api, "current_user", refuse)

    response = client.post("/setup/github/poll")

    assert response.status_code == 400
    assert "403" in response.json()["error"]


def test_a_finished_flow_is_not_left_pending(monkeypatch):
    """Whatever happened, the device code is spent. Keep it and the next poll
    asks GitHub about a code it has already answered."""
    from web.routers import github as page

    monkeypatch.setitem(page._pending, "device_code", "dev_code")
    monkeypatch.setattr(page.device, "poll_once", lambda client_id, code: device.Answer(token="gho_new"))
    monkeypatch.setattr(page, "set_secret", lambda name, value: None)
    monkeypatch.setattr(page.api, "current_user", lambda token: "sud018")

    client.post("/setup/github/poll")

    assert page._pending == {}


def test_a_pasted_github_url_is_stored_as_owner_slash_repo():
    """The field says owner/repository, but it sits under a page full of links."""
    client.post("/setup/github/client",
                data={"client_id": "Iv1.abc", "repo": "https://github.com/me/agent"},
                follow_redirects=False)
    assert load_config().github_repo == "me/agent"


def test_something_that_is_not_a_repository_is_refused_and_changes_nothing():
    client.post("/setup/github/client",
                data={"client_id": "Iv1.abc", "repo": "me/agent"}, follow_redirects=False)

    response = client.post("/setup/github/client",
                           data={"client_id": "", "repo": "https://github.com/me"},
                           follow_redirects=False)

    assert "error=" in response.headers["location"]
    assert load_config().github_repo == "me/agent"


def test_a_blank_repo_still_leaves_the_stored_one_alone():
    client.post("/setup/github/client",
                data={"client_id": "Iv1.abc", "repo": "me/agent"}, follow_redirects=False)
    client.post("/setup/github/client",
                data={"client_id": "Iv1.xyz", "repo": ""}, follow_redirects=False)
    cfg = load_config()
    assert cfg.github_repo == "me/agent"
    assert cfg.github_client_id == "Iv1.xyz"
