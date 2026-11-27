"""The web app: the API under /api (read-only, except the live session's start, control and decide) and the front end at /."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import day as day_data
from backend.api import live as live_api
from backend.settings import ROOT

FRONTEND = ROOT / "frontend"


@asynccontextmanager
async def lifespan(_app):
    """No live session survives a restart: any left running or paused is marked stopped (read-only in history)."""
    from simulator.live import stop_orphans
    try:
        with live_api._db() as conn:
            stop_orphans(conn)
    except Exception:                                    # noqa: BLE001 - no database yet: nothing to mark
        pass
    yield


app = FastAPI(title="Ground-Stop Co-Pilot", docs_url="/api/docs", openapi_url="/api/openapi.json", lifespan=lifespan)
app.include_router(live_api.router)


@app.exception_handler(live_api.LiveError)
async def _live_error(request, exc):
    """Every live error: detail is one plain sentence; extra facts beside it."""
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": exc.detail, **exc.extra}, status_code=exc.status_code)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request, exc):
    from fastapi.responses import JSONResponse
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail)
    return JSONResponse({"detail": detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _invalid(request, exc):
    """Bad input: one plain sentence naming each field that is wrong (never a list of objects)."""
    from fastapi.responses import JSONResponse
    parts = []
    for e in exc.errors():
        where = ".".join(str(x) for x in e.get("loc", ()) if x not in ("body", "query", "path"))
        parts.append(f"{where}: {e.get('msg')}" if where else str(e.get("msg")))
    return JSONResponse({"detail": "Invalid request - " + "; ".join(parts)}, status_code=422)


@app.get("/api/runs")
def runs():
    return day_data.runs()


@app.get("/api/day/{run_id}")
def day(run_id: str):
    known = {r["run_id"] for r in day_data.runs()}
    if run_id not in known:
        raise HTTPException(404, f"No run called {run_id}. Run the simulator first.")
    return day_data.day(run_id)


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/", StaticFiles(directory=FRONTEND), name="frontend")
