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
    assert "passengers" not in src and "full_name" not in src and "booking_ref" not in src
