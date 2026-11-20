"""Settings read from the .env file."""
import os
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DATABASE_URL = os.getenv("DATABASE_URL", "")
SIM_DATE = date.fromisoformat(os.getenv("SIM_DATE", "2026-10-06"))
TZ = ZoneInfo(os.getenv("SIM_TIMEZONE", "America/Chicago"))
SEED = int(os.getenv("SEED", "42"))
SAMPLE_PASSENGERS = int(os.getenv("SAMPLE_PASSENGERS", "300"))

RAW = ROOT / "data" / "raw"
SCHEMA = ROOT / "backend" / "db" / "schema.sql"
