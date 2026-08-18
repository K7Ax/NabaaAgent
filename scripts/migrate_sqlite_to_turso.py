from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from opportunity_sentinel.repository import Repository

TABLES: tuple[str, ...] = (
    "students",
    "opportunities",
    "sources",
    "taxonomy",
    "saved_opportunities",
    "deliveries",
    "crawl_runs",
    "raw_signals",
    "opportunity_versions",
    "opportunity_sources",
    "evidence_records",
    "matches",
    "delivery_queue",
    "admin_reviews",
    "quota_usage",
    "health_events",
    "profile_answers",
    "digests",
)


def _identifier(value: str) -> str:
    if value not in TABLES and not value.replace("_", "").isalnum():
        raise ValueError(f"Unsafe SQLite identifier: {value!r}")
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _table_columns(connection: Any, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({_identifier(table)})")]


def _chunks(rows: Sequence[Sequence[Any]], size: int = 250):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def copy_database(
    source: sqlite3.Connection,
    destination: Any,
    progress: Callable[[str, int], None] | None = None,
) -> dict[str, int]:
    source_tables = {
        row[0]
        for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    copied: dict[str, int] = {}
    destination.execute("PRAGMA foreign_keys=OFF")
    try:
        for table in TABLES:
            if table not in source_tables:
                continue
            source_columns = _table_columns(source, table)
            destination_columns = set(_table_columns(destination, table))
            columns = [column for column in source_columns if column in destination_columns]
            if not columns:
                continue

            quoted_columns = ", ".join(_identifier(column) for column in columns)
            rows = [
                tuple(row)
                for row in source.execute(
                    f"SELECT {quoted_columns} FROM {_identifier(table)}"
                ).fetchall()
            ]
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                statement = (
                    f"INSERT OR REPLACE INTO {_identifier(table)} "
                    f"({quoted_columns}) VALUES ({placeholders})"
                )
                for batch in _chunks(rows):
                    destination.executemany(statement, batch)
            copied[table] = len(rows)
            if progress:
                progress(table, len(rows))
        destination.commit()
    finally:
        destination.execute("PRAGMA foreign_keys=ON")

    violations = destination.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise RuntimeError(f"Foreign-key verification failed with {len(violations)} violation(s)")
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate NabaaAgent SQLite data to Turso")
    parser.add_argument("--source", type=Path, default=Path("opportunity_sentinel.db"))
    args = parser.parse_args()

    database_url = os.environ.get("DATABASE_URL", "")
    auth_token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not database_url.startswith("libsql://") or not auth_token:
        raise SystemExit("DATABASE_URL and TURSO_AUTH_TOKEN are required")
    if not args.source.is_file():
        raise SystemExit(f"Source database does not exist: {args.source}")

    source = sqlite3.connect(args.source)
    destination = Repository(
        Path(":memory:"), database_url=database_url, auth_token=auth_token
    )
    try:
        copied = copy_database(
            source,
            destination.connection,
            progress=lambda table, count: print(
                json.dumps({"table": table, "copied": count}), flush=True
            ),
        )
    finally:
        source.close()
        destination.connection.close()
    print(json.dumps({"status": "ok", "copied": copied}, ensure_ascii=False))


if __name__ == "__main__":
    main()
