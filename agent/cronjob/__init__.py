"""Keeping an outside scheduler in step with the posting schedule.

GitHub's own cron is best-effort and has been running hours late. A scheduling
service fixes that, but introduces a third copy of the schedule - after
config.json and post.yml - and a copy nobody checks is a copy that goes stale
silently. This package removes the copy: the app owns the service's jobs and
reconciles them whenever the schedule changes.
"""
