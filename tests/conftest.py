import pytest

from backend.database import connect
from scripts.reset_db import main as reset


class _Connection:
    """One database connection for the whole run. The cloud database closes a connection that sits idle for a long time (the
    long in-memory simulations run between database tests), so each test checks it first and reconnects if it was closed."""

    def __init__(self):
        self.conn = None

    def cursor(self):
        if self.conn is not None:
            try:
                with self.conn.cursor() as cur:
                    cur.execute("SELECT 1")
            except Exception:
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
        if self.conn is None:
            self.conn = connect()
            self.conn.autocommit = True      # no transaction left open, so later tests can rebuild the database
        return self.conn.cursor()


@pytest.fixture(scope="session")
def _database():
    """Rebuild the database once per run."""
    reset()
    holder = _Connection()
    yield holder
    if holder.conn is not None:
        holder.conn.close()


@pytest.fixture
def db(_database):
    """Hand every test a cursor on a live connection."""
    with _database.cursor() as cur:
        yield cur


def one(cur, sql, *args):
    cur.execute(sql, args)
    return cur.fetchone()[0]
