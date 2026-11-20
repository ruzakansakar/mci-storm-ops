"""Wipe the database and rebuild it from the class files.

Run:  python -m scripts.reset_db
Safe to run as many times as you like; the same SEED always gives the same data.
"""
from backend.database import connect
from backend.seed.build import build
from backend.seed.load import load
from backend.settings import SCHEMA, SEED


def main():
    data = build()
    with connect() as conn:
        with conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS public CASCADE")
            cur.execute("CREATE SCHEMA public")
            cur.execute(SCHEMA.read_text())
        counts = load(conn, data)
        conn.commit()
    print(f"Database rebuilt (seed {SEED}).")
    for table, n in counts.items():
        print(f"  {table:<18} {n:>5} rows")
    print(f"\n{len(data['data_issues'])} data issues recorded. Run `python -m scripts.summary` to read them.")


if __name__ == "__main__":
    main()
