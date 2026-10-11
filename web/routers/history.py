"""Every run the agent has made, with the failures kept in.

The dashboard shows five, because a dashboard is a glance. This page is the
archive - and it keeps the skipped and failed runs rather than only the posts,
because "what happened on the 28th" is usually asked when something did not.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from agent.state import load_state
from agent.status import counts
from web.app import templates
from web.deps import link_for

router = APIRouter(prefix="/history")

FILTERS = [("", "Everything"), ("posted", "Posted"),
           ("skipped", "Skipped"), ("failed", "Failed")]


@router.get("", response_class=HTMLResponse)
def page(request: Request, status: str = "") -> HTMLResponse:
    history = load_state().history
    rows = [e for e in history if e.status == status] if status else list(history)

    return templates.TemplateResponse(
        request=request,
        name="pages/history.html",
        context={
            "rows": list(reversed(rows)),          # newest first
            "total": len(history),
            "tally": counts(load_state()),
            "filters": FILTERS,
            "status": status,
            "link_for": link_for,
        },
    )
