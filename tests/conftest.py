import pytest

from backend.database import connect
from scripts.reset_db import main as reset


@pytest.fixture(scope="session")
def db():
    """Rebuild the database once, then hand every test a cursor."""
    reset()
    with connect() as conn:
        conn.autocommit = True      # no transaction left open, so later tests can rebuild the database
        with conn.cursor() as cur:
            yield cur


def one(cur, sql, *args):
    cur.execute(sql, args)
    return cur.fetchone()[0]
