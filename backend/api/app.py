"""The web app: the API under /api (read-only, except the live session's start, control and decide) and the front end at /."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
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
