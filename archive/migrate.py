"""Apply numbered SQL migrations, in order, once each.

A migration is applied inside a transaction and recorded in schema_migrations
with a checksum of the file. If a file changes after it has been applied, this
refuses to run rather than pretending the database matches the repository.

A file whose first lines contain `-- migrate:no-transaction` is applied with
autocommit, because some TimescaleDB statements (creating a continuous
aggregate, adding a policy) cannot run inside a transaction block.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DEFAULT_DSN = "postgresql://crpms:crpms@localhost:5432/crpms"

TRACKING = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text        PRIMARY KEY,
    filename   text        NOT NULL,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def dsn() -> str:
    return os.environ.get("CRPMS_DSN", DEFAULT_DSN)


def discover() -> list[Path]:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise SystemExit(f"no migrations found in {MIGRATIONS_DIR}")
    return files


def version_of(path: Path) -> str:
    return path.name.split("_", 1)[0]


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def applied(conn: psycopg.Connection) -> dict[str, tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT version, filename, checksum FROM schema_migrations")
        return {v: (f, c) for v, f, c in cur.fetchall()}


def migrate(conn: psycopg.Connection, dry_run: bool = False) -> int:
    with conn.cursor() as cur:
        cur.execute(TRACKING)
    conn.commit()

    done = applied(conn)
    pending = 0

    for path in discover():
        version = version_of(path)
        digest = checksum(path)

        if version in done:
            _, recorded = done[version]
            if recorded != digest:
                raise SystemExit(
                    f"migration {path.name} has changed since it was applied "
                    f"(recorded {recorded}, now {digest}). Migrations are "
                    f"immutable once applied; add a new one instead."
                )
            continue

        pending += 1
        if dry_run:
            print(f"  PENDING {path.name}")
            continue

        sql = path.read_text()
        no_tx = "-- migrate:no-transaction" in sql.split("\n\n", 1)[0]
        print(f"  applying {path.name}{' (autocommit)' if no_tx else ''} ... ",
              end="", flush=True)

        if no_tx:
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO schema_migrations (version, filename, checksum)"
                        " VALUES (%s, %s, %s)", (version, path.name, digest))
            finally:
                conn.autocommit = False
        else:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, filename, checksum)"
                    " VALUES (%s, %s, %s)", (version, path.name, digest))
            conn.commit()
        print("ok")

    return pending


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="archive.migrate")
    ap.add_argument("--status", action="store_true", help="list without applying")
    args = ap.parse_args(argv)

    with psycopg.connect(dsn()) as conn:
        if args.status:
            with conn.cursor() as cur:
                cur.execute(TRACKING)
            conn.commit()
            done = applied(conn)
            for path in discover():
                mark = "APPLIED" if version_of(path) in done else "pending"
                print(f"  {mark:<8} {path.name}")
            return 0

        pending = migrate(conn)
        print(f"{pending} migration(s) applied" if pending
              else "database is up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
