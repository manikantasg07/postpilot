"""The GitHub layer: reading the remote, sealing secrets, and the device flow."""
from __future__ import annotations

import base64

import pytest
from nacl import encoding, public

from agent.github import api, device, repo_secrets
from agent.github.errors import GitHubAuthError, GitHubError

# --- finding the repo ------------------------------------------------------

HTTPS = '[remote "origin"]\n\turl = https://github.com/manikanta/linkedin-agent.git\n'
SSH = '[remote "origin"]\n\turl = git@github.com:manikanta/linkedin-agent.git\n'
GITLAB = '[remote "origin"]\n\turl = https://gitlab.com/manikanta/linkedin-agent.git\n'


@pytest.mark.parametrize("text", [HTTPS, SSH, HTTPS.replace(".git", "")])
def test_repo_is_read_from_the_git_remote(tmp_path, text):
    config = tmp_path / "config"
    config.write_text(text, encoding="utf-8")
    assert api.repo_from_remote(config) == "manikanta/linkedin-agent"


def test_missing_git_config_is_not_an_error(tmp_path):
    assert api.repo_from_remote(tmp_path / "nothing") == ""


def test_a_remote_somewhere_else_is_ignored(tmp_path):
    config = tmp_path / "config"
    config.write_text(GITLAB, encoding="utf-8")
    assert api.repo_from_remote(config) == ""


# --- sealing secrets -------------------------------------------------------

def test_sealed_value_can_only_be_opened_with_the_private_key():
    private = public.PrivateKey.generate()
    key_b64 = private.public_key.encode(encoding.Base64Encoder()).decode()

    locked = repo_secrets.seal(key_b64, "sk-super-secret")

    opened = public.SealedBox(private).decrypt(base64.b64decode(locked))
    assert opened.decode() == "sk-super-secret"


def test_sealing_the_same_value_twice_looks_different():
    """Sealed boxes are randomised, so the ciphertext leaks nothing."""
    private = public.PrivateKey.generate()
    key_b64 = private.public_key.encode(encoding.Base64Encoder()).decode()

    assert repo_secrets.seal(key_b64, "same") != repo_secrets.seal(key_b64, "same")


# --- the device flow -------------------------------------------------------

class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


def test_start_needs_a_client_id():
    with pytest.raises(GitHubAuthError):
        device.start("")


def test_start_returns_what_the_page_shows(monkeypatch):
    monkeypatch.setattr(device.requests, "post", lambda *a, **k: FakeResponse(
        {"device_code": "long", "user_code": "ABCD-1234", "interval": 5,
         "verification_uri": "https://github.com/login/device", "expires_in": 900}))

    flow = device.start("Iv1.test")
    assert flow["user_code"] == "ABCD-1234"
    assert flow["device_code"] == "long"


def test_pending_approval_is_not_an_error(monkeypatch):
    monkeypatch.setattr(device.requests, "post",
                        lambda *a, **k: FakeResponse({"error": "authorization_pending"}))
    assert device.poll_once("Iv1.test", "long").waiting


def test_slow_down_also_just_means_keep_waiting(monkeypatch):
    monkeypatch.setattr(device.requests, "post",
                        lambda *a, **k: FakeResponse({"error": "slow_down"}))
    assert device.poll_once("Iv1.test", "long").waiting


def test_approval_gives_back_the_token(monkeypatch):
    monkeypatch.setattr(device.requests, "post",
                        lambda *a, **k: FakeResponse({"access_token": "gho_abc"}))
    assert device.poll_once("Iv1.test", "long").token == "gho_abc"


def test_a_refusal_stops_the_polling(monkeypatch):
    monkeypatch.setattr(device.requests, "post",
                        lambda *a, **k: FakeResponse({"error": "access_denied"}))
    with pytest.raises(GitHubAuthError):
        device.poll_once("Iv1.test", "long")


def test_expired_code_says_so_in_plain_words(monkeypatch):
    monkeypatch.setattr(device.requests, "post",
                        lambda *a, **k: FakeResponse({"error": "expired_token"}))
    with pytest.raises(GitHubAuthError, match="expired"):
        device.poll_once("Iv1.test", "long")


# --- the API helper --------------------------------------------------------

def test_a_rejected_token_is_an_auth_error(monkeypatch):
    monkeypatch.setattr(api.requests, "request", lambda *a, **k: FakeResponse({}, status=401))
    with pytest.raises(GitHubAuthError):
        api.call("GET", "/user", "bad")


def test_other_failures_keep_the_status_code(monkeypatch):
    monkeypatch.setattr(api.requests, "request", lambda *a, **k: FakeResponse({}, status=500))
    with pytest.raises(GitHubError, match="500"):
        api.call("GET", "/user", "token")


def test_slow_down_is_told_apart_from_still_waiting(monkeypatch):
    """Both mean "no token yet", but only one is fixed by asking less often."""
    monkeypatch.setattr(device.requests, "post",
                        lambda *a, **k: FakeResponse({"error": "slow_down"}))

    answer = device.poll_once("Iv1.test", "long")

    assert answer.waiting and answer.too_fast


def test_plain_waiting_is_not_a_back_off(monkeypatch):
    monkeypatch.setattr(device.requests, "post",
                        lambda *a, **k: FakeResponse({"error": "authorization_pending"}))

    answer = device.poll_once("Iv1.test", "long")

    assert answer.waiting and not answer.too_fast


# --- several files, one commit ----------------------------------------------

def test_put_files_builds_one_commit_and_moves_the_branch(monkeypatch):
    from agent.github import api

    calls = []

    def fake(method, path, token, **kw):
        calls.append(f"{method} {path}")
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "head1"}}
        if "/git/commits/head1" in path:
            return {"tree": {"sha": "tree0"}}
        if path.endswith("/git/trees"):
            assert kw["json"]["base_tree"] == "tree0"
            return {"sha": "tree1"}
        if path.endswith("/git/commits"):
            assert kw["json"]["parents"] == ["head1"]
            assert kw["json"]["tree"] == "tree1"
            return {"sha": "commit1"}
        if "/git/refs/heads/main" in path:
            assert kw["json"]["sha"] == "commit1"
            return {}
        return {"default_branch": "main"}

    monkeypatch.setattr(api, "call", fake)

    sha = api.put_files("me/agent", {"a.json": "1", "b.yml": "2"}, "one commit", "tok")

    assert sha == "commit1"
    assert sum(1 for c in calls if c == "POST /repos/me/agent/git/commits") == 1


def test_put_files_does_nothing_when_nothing_changed(monkeypatch):
    from agent.github import api
    monkeypatch.setattr(api, "call", lambda *a, **k: pytest.fail("should not call GitHub"))
    assert api.put_files("me/agent", {}, "no-op", "tok") == ""


# --- the repository box accepts what people actually paste -------------------
# A pasted browser URL used to be stored verbatim, so the next push asked for
# /repos/https://github.com/owner/repo/contents/... and GitHub answered 404
# with nothing in the message pointing at the real mistake.

@pytest.mark.parametrize("typed", [
    "me/agent",
    "  me/agent  ",
    "https://github.com/me/agent",
    "https://github.com/me/agent/",
    "http://github.com/me/agent",
    "https://www.github.com/me/agent",
    "github.com/me/agent",
    "https://github.com/me/agent.git",
    "git@github.com:me/agent.git",
])
def test_normalise_repo_accepts_every_form_of_the_same_repo(typed):
    from agent.github import api
    assert api.normalise_repo(typed) == "me/agent"


@pytest.mark.parametrize("typed", [
    "",
    "agent",
    "https://github.com/me",
    "https://github.com/me/agent/tree/main",
    "https://gitlab.com/me/agent",
    "https://github.com/me/agent/issues/4",
])
def test_normalise_repo_refuses_what_is_not_a_repository(typed):
    from agent.github import api
    assert api.normalise_repo(typed) == ""


def test_a_repo_with_dots_and_dashes_survives():
    from agent.github import api
    assert api.normalise_repo("https://github.com/my-org/my.cool-repo") == "my-org/my.cool-repo"
