"""Errors from talking to cron-job.org."""


class CronJobError(RuntimeError):
    """Something went wrong calling the cron-job.org API."""


class CronJobAuthError(CronJobError):
    """The API key is missing, wrong, or was revoked."""


class CronJobBusyError(CronJobError):
    """Too many requests. Nothing is wrong - we asked too often."""
