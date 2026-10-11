"""The small slice of the cron-job.org REST API this app needs.

https://docs.cron-job.org/rest-api.html
"""
from __future__ import annotations

import requests

from agent.cronjob.errors import CronJobAuthError, CronJobBusyError, CronJobError

API = "https://api.cron-job.org"

# Errors carry the status code and nothing else. The request body we send holds
# the GitHub dispatch token, and a service that quotes the offending payload
# back at us would put that token into an error string - which the page then
# urlencodes into a redirect, where it lands in browser history.
TIMEOUT = 30

# The API takes the HTTP verb as a number. Only POST is ever needed here, but
# naming it beats a bare 1 in the payload.
METHOD_POST = 1

# "every value" for a schedule field - their wildcard.
EVERY = [-1]


def call(method: str, path: str, key: str, **kwargs) -> dict:
    """One place for headers, timeouts and error messages."""
    response = requests.request(
        method,
        f"{API}{path}",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"},
        timeout=TIMEOUT,
        **kwargs,
    )

    if response.status_code in (401, 403):
        raise CronJobAuthError(
            "cron-job.org refused the API key. Check it in their console under "
            "Settings, and paste it again here."
        )
    if response.status_code == 429:
        raise CronJobBusyError(
            "cron-job.org is rate limiting us. Nothing is wrong with your cronjobs; "
            "the app just asked too often. It will try again shortly."
        )
    if response.status_code >= 300:
        raise CronJobError(
            f"cron-job.org {method} {path} failed ({response.status_code})."
        )

    return response.json() if response.content else {}


def list_jobs(key: str) -> list[dict]:
    """Every job on the account, ours and theirs alike."""
    return call("GET", "/jobs", key).get("jobs", [])


def get_job(job_id: int, key: str) -> dict:
    """One job in full, including the headers and body the listing omits.

    Used when something really needs verifying. Not used by the routine drift
    check: that runs on every page load, and one call per job was enough to
    get the whole app rate limited.
    """
    data = call("GET", f"/jobs/{job_id}", key)
    return data.get("jobDetails") or data.get("job") or data


def create_job(job: dict, key: str) -> int:
    """Add a job. Returns the id the service assigned it."""
    return call("PUT", "/jobs", key, json={"job": job})["jobId"]


def update_job(job_id: int, changes: dict, key: str) -> None:
    """Change only the fields given; everything else is left alone."""
    call("PATCH", f"/jobs/{job_id}", key, json={"job": changes})


def delete_job(job_id: int, key: str) -> None:
    call("DELETE", f"/jobs/{job_id}", key)


def whoami(key: str) -> int:
    """No /user endpoint exists, so a successful listing is the proof.

    Returns how many jobs the account has, which is the only thing worth
    showing after a connection test anyway.
    """
    return len(list_jobs(key))
