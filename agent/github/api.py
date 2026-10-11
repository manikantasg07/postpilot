"""The small slice of the GitHub REST API this app needs."""
from __future__ import annotations

import base64
import re
from pathlib import Path

import requests

from agent import paths
from agent.github.errors import GitHubAuthError, GitHubError

API = "https://api.github.com"
TIMEOUT = 30

# https://github.com/owner/repo.git  and  git@github.com:owner/repo.git
REMOTE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?$")

# What a person actually pastes into the repository box. The field asks for
# "owner/repo", but it sits directly under a paragraph of github.com links, so
# the address bar is the obvious thing to copy. Accepting both costs one regex;
# rejecting the URL cost a 404 reading
# "/repos/https://github.com/owner/repo/contents/..." with no hint of the cause.
REPO_INPUT = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?(?:[\w.-]+@)?github\.com[/:])?"
    r"([\w.-]+)/([\w.-]+?)(?:\.git)?/?$"
)


def normalise_repo(text: str) -> str:
    """Turn anything that names a GitHub repository into "owner/repo".

    Accepts the browser URL, the clone URL, the SSH form, a trailing slash or
    .git, and the plain owner/repo the field asks for. Returns "" for anything
    else, so the caller can say so rather than building a broken request.
    """
    match = REPO_INPUT.match(text.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else ""


def call(method: str, path: str, token: str, **kwargs) -> dict | list:
    """One place for headers, timeouts and error messages."""
    response = requests.request(
        method,
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=TIMEOUT,
        **kwargs,
    )

    if response.status_code in (401, 403):
        raise GitHubAuthError(
            f"GitHub refused the token ({response.status_code}). "
            "Reconnect, and make sure the app may read and write this repository."
        )
    if response.status_code >= 300:
        # No response body: these messages are shown to the user and end up in
        # redirect query strings, and an API that echoes the request back would
        # put a token there. The status code is what tells you what to do.
        raise GitHubError(f"GitHub {method} {path} failed ({response.status_code}).")

    return response.json() if response.content else {}


def current_user(token: str) -> str:
    """The login name the token belongs to - proof the connection works."""
    return call("GET", "/user", token)["login"]


def repo_from_remote(git_config: Path | None = None) -> str:
    """Read 'owner/repo' out of .git/config, so nobody has to type it.

    Returns "" when this folder is not a git repo, or has no GitHub remote.
    """
    git_config = git_config or paths.ROOT / ".git" / "config"
    if not git_config.exists():
        return ""

    for line in git_config.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("url"):
            continue
        match = REMOTE.search(line.split("=", 1)[-1].strip())
        if match:
            return f"{match.group(1)}/{match.group(2)}"
    return ""


def get_file(repo: str, path: str, token: str) -> tuple[str, str]:
    """(text, sha) of a file in the repo. ("", "") when it does not exist yet."""
    try:
        data = call("GET", f"/repos/{repo}/contents/{path}", token)
    except GitHubError as exc:
        if "404" in str(exc):
            return "", ""
        raise
    content = base64.b64decode(data.get("content", "")).decode("utf-8")
    return content, data.get("sha", "")


def put_file(repo: str, path: str, text: str, message: str, token: str) -> str:
    """Create or update one file, and return the new commit's sha.

    GitHub needs the old file's sha to update it - that is how it refuses to
    overwrite a change somebody else pushed in the meantime.
    """
    _, sha = get_file(repo, path, token)
    body = {
        "message": message,
        "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
    }
    if sha:
        body["sha"] = sha

    data = call("PUT", f"/repos/{repo}/contents/{path}", token, json=body)
    return data.get("commit", {}).get("sha", "")


def default_branch(repo: str, token: str) -> str:
    return call("GET", f"/repos/{repo}", token).get("default_branch", "main")


def put_files(repo: str, files: dict[str, str], message: str, token: str,
              branch: str = "") -> str:
    """Write several files as ONE commit. Returns the new commit's sha.

    put_file() writes one file per commit, which is fine on its own and wrong
    for a set that has to agree. config.json and post.yml describe the same
    schedule, so committing them separately leaves a commit in between where
    they contradict each other - and that commit fails the cron-drift test,
    turning CI red and sending a failure notification for a change that was
    perfectly correct.

    This is the plumbing API rather than the contents API: build a tree from
    the current one, hang a commit off it, then move the branch.
    """
    if not files:
        return ""

    branch = branch or default_branch(repo, token)
    ref = call("GET", f"/repos/{repo}/git/ref/heads/{branch}", token)
    head = ref["object"]["sha"]
    base_tree = call("GET", f"/repos/{repo}/git/commits/{head}", token)["tree"]["sha"]

    tree = call("POST", f"/repos/{repo}/git/trees", token, json={
        "base_tree": base_tree,
        "tree": [{"path": path, "mode": "100644", "type": "blob", "content": text}
                 for path, text in sorted(files.items())],
    })

    commit = call("POST", f"/repos/{repo}/git/commits", token, json={
        "message": message, "tree": tree["sha"], "parents": [head],
    })

    call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", token,
         json={"sha": commit["sha"]})
    return commit["sha"]


def recent_runs(repo: str, token: str, limit: int = 5) -> list[dict]:
    """The latest workflow runs, trimmed to what the page shows."""
    data = call("GET", f"/repos/{repo}/actions/runs?per_page={limit}", token)
    return [
        {
            "name": run.get("name", ""),
            "status": run.get("status", ""),
            "conclusion": run.get("conclusion") or "",
            "started": (run.get("run_started_at") or "")[:16].replace("T", " "),
            "url": run.get("html_url", ""),
        }
        for run in data.get("runs", data.get("workflow_runs", []))
    ]
