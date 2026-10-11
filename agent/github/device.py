"""GitHub's device flow: sign in without ever typing a password here.

The dance:
  1. We ask GitHub for a pair of codes. You get a short one to type; we keep a
     long one nobody sees.
  2. You open github.com/login/device, type the short code, and approve.
  3. We keep asking GitHub "has he approved yet?" until it hands us a token.

Nothing sensitive passes through this app: the approval happens on github.com.
"""
from __future__ import annotations

from dataclasses import dataclass

import requests

from agent.github.errors import GitHubAuthError, GitHubError

CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
VERIFY_URL = "https://github.com/login/device"
GRANT = "urn:ietf:params:oauth:grant-type:device_code"

# repo   - read and write files in the repository
# workflow - update .github/workflows/post.yml
SCOPE = "repo workflow"

TIMEOUT = 30
JSON = {"Accept": "application/json"}

# What GitHub says while it waits for you, versus what has actually gone wrong.
#
# "slow_down" is not the same as "authorization_pending". It means we asked too
# often, and GitHub's rule is that the caller must then wait five seconds
# longer than before. Treating the two alike means polling at a rate GitHub has
# already refused, so it keeps answering slow_down and the token is never
# handed over - the page waits for ever with nothing visibly wrong.
WAITING = {"authorization_pending", "slow_down"}
BACK_OFF = "slow_down"
EXTRA_SECONDS = 5
MESSAGES = {
    "expired_token": "The code expired. Start again.",
    "access_denied": "You cancelled the approval on GitHub.",
    "incorrect_device_code": "That code is no longer valid. Start again.",
    "unsupported_grant_type": "This OAuth app does not have device flow switched on.",
}


def _post(url: str, data: dict) -> dict:
    response = requests.post(url, data=data, headers=JSON, timeout=TIMEOUT)
    if response.status_code >= 300:
        raise GitHubError(f"GitHub returned {response.status_code}: {response.text[:200]}")
    return response.json()


def start(client_id: str) -> dict:
    """Ask for the code pair. Returns what the page needs to show you."""
    if not client_id:
        raise GitHubAuthError("No GitHub client ID yet.")

    data = _post(CODE_URL, {"client_id": client_id, "scope": SCOPE})
    if "device_code" not in data:
        raise GitHubAuthError(
            MESSAGES.get(data.get("error", ""), f"GitHub refused the client ID: {data}")
        )

    return {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri", VERIFY_URL),
        "interval": int(data.get("interval", 5)),
        "expires_in": int(data.get("expires_in", 900)),
    }


@dataclass
class Answer:
    """One reply from GitHub: the token, or why not yet."""

    token: str = ""
    reason: str = ""          # "authorization_pending" or "slow_down"

    @property
    def waiting(self) -> bool:
        return not self.token

    @property
    def too_fast(self) -> bool:
        return self.reason == BACK_OFF


def poll_once(client_id: str, device_code: str) -> Answer:
    """One "are we there yet?".

    Raises when the answer is a real refusal, so the page can stop polling.
    """
    data = _post(TOKEN_URL, {"client_id": client_id, "device_code": device_code, "grant_type": GRANT})

    token = data.get("access_token")
    if token:
        return Answer(token=token)

    error = data.get("error", "")
    if error in WAITING:
        return Answer(reason=error)

    raise GitHubAuthError(MESSAGES.get(error, f"GitHub said: {error or data}"))
