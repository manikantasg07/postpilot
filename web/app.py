"""FastAPI application factory."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from agent import __version__
from agent.config import ConfigError
from agent.jsonio import DataFileError
from agent.secrets_store import SecretsUnavailable

WEB_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def static_stamp() -> str:
    """A value that changes whenever the CSS or JS does.

    Every <link> and <script> carries it as ?v=..., so a browser cannot serve
    yesterday's stylesheet from its cache. Without this, a fix that never
    reaches the page is indistinguishable from a fix that does not work - and
    "hard-refresh and try again" is not a debugging step anyone should need.

    The app version alone is not enough: it changes on release, and the files
    change far more often than that.
    """
    newest = max((path.stat().st_mtime for path in (WEB_DIR / "static").rglob("*")
                  if path.is_file()), default=0)
    return f"{__version__}-{int(newest)}"


templates.env.globals["version"] = static_stamp()


def create_app() -> FastAPI:
    app = FastAPI(title="PostCadence", version=__version__, docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    from web.routers import (
        dashboard,
        drafts,
        github,
        history,
        linkedin,
        schedule,
        settings,
        sync,
        topics,
        trigger,
        wizard,
    )

    app.include_router(dashboard.router)
    app.include_router(history.router)
    app.include_router(topics.router)
    app.include_router(wizard.router)
    app.include_router(drafts.router)
    app.include_router(github.router)
    app.include_router(linkedin.router)
    app.include_router(schedule.router)
    app.include_router(settings.router)
    app.include_router(sync.router)
    app.include_router(trigger.router)

    # One net under every page. A broken or half-written data file used to
    # reach the browser as a 500 with a traceback and no way back - and the
    # one page that handled it was the dashboard, which is not where people
    # were when they hit it.
    @app.exception_handler(ConfigError)
    @app.exception_handler(DataFileError)
    @app.exception_handler(FileNotFoundError)
    @app.exception_handler(SecretsUnavailable)
    def _cannot_read_your_settings(request: Request, exc: Exception) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request, name="pages/needs_setup.html",
            context={"error": str(exc)}, status_code=200)

    return app


app = create_app()
