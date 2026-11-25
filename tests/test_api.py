"""Front end step 0: the read-only API and the map data."""
import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.settings import ROOT

client = TestClient(app)


def test_every_station_has_map_coordinates(db):
    coords = yaml.safe_load((ROOT / "config" / "airports.yaml").read_text(encoding="utf-8"))
    db.execute("SELECT code FROM stations")
    missing = [c for (c,) in db.fetchall() if c not in coords]
    assert missing == []


def test_front_end_page_is_served():
    r = client.get("/")
    assert r.status_code == 200 and "Ground-Stop Co-Pilot" in r.text


def test_runs_endpoint_answers(db):
    r = client.get("/api/runs")
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_unknown_run_is_404(db):
    assert client.get("/api/day/no-such-run").status_code == 404


def test_api_is_read_only_and_never_reads_passenger_details():
    src = (ROOT / "backend" / "api" / "day.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE)\b", src, re.I)
    assert not re.search(r"\b(FROM|JOIN)\s+passengers\b", src, re.I)   # counts on a picture are fine, the table is not
    assert "full_name" not in src and "booking_ref" not in src


def test_day_carries_situation_pictures_and_weather_inputs(db):
    db.execute("INSERT INTO events (occurred_at, type, actor, actor_name, run_id, payload) "
               "VALUES ('2026-10-06 11:30-05', 'ground_stop_started', 'simulator', 'faa', 'api-test-today', "
               "'{\"reason\": \"thunderstorms\", \"program\": \"ground_stop\"}')")
    db.connection.commit()
    body = client.get("/api/day/api-test-today").json()    # events are append-only; the test database is reset each run
    assert body["situation"] == []                          # no co-pilot in this run, so no picture
    assert body["world"][0]["type"] == "ground_stop_started" and body["world"][0]["t"] == 690
