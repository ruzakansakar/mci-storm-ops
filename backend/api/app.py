"""The web app: a read-only API under /api and the front end at /."""
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from backend.api import day as day_data
from backend.settings import ROOT

FRONTEND = ROOT / "frontend"

app = FastAPI(title="Ground-Stop Co-Pilot", docs_url="/api/docs", openapi_url="/api/openapi.json")


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
