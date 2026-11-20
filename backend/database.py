"""One place to open a database connection."""
import psycopg

from backend.settings import DATABASE_URL


def connect() -> psycopg.Connection:
    if not DATABASE_URL or "USER:PASSWORD" in DATABASE_URL:
        raise SystemExit(
            "DATABASE_URL is not set. Copy .env.example to .env and paste your Neon connection string."
        )
    return psycopg.connect(DATABASE_URL)
