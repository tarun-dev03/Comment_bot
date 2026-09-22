from contextlib import asynccontextmanager
import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routes import web

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db_error = None
    try:
        await init_db()
    except Exception as exc:
        app.state.db_error = str(exc)
        logger.exception("Startup: database unavailable")
    yield


app = FastAPI(title="YouTube Live Comment Bot", lifespan=lifespan)
app.include_router(web.router)


@app.get("/ready")
async def ready(request: Request):
    err = getattr(request.app.state, "db_error", None)
    if err:
        return HTMLResponse(
            f"<h1>Database not ready</h1><p>{err}</p>"
            "<p>On Render: link Postgres DATABASE_URL to this service and redeploy.</p>",
            status_code=503,
        )
    return {"ok": True, "db": True}


static_dir = Path(__file__).resolve().parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
