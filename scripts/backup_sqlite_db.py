#!/usr/bin/env python3
"""Create a consistent, atomic SQLite backup, including committed WAL data."""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


def backup_database(source: str | Path, destination: str | Path) -> Path:
    source_path = Path(source).expanduser().resolve(strict=True)
    destination_path = Path(destination).expanduser()
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source is not a file: {source_path}")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".tmp", dir=destination_path.parent
    )
    os.close(fd)
    temp_path = Path(temp_name)
    source_uri = source_path.as_uri() + "?mode=ro"

    try:
        source_conn = sqlite3.connect(source_uri, uri=True, timeout=30.0)
        try:
            destination_conn = sqlite3.connect(str(temp_path), timeout=30.0)
            try:
                source_conn.backup(destination_conn)
                check = destination_conn.execute("PRAGMA quick_check").fetchone()
                if not check or check[0] != "ok":
                    raise sqlite3.DatabaseError(f"Backup quick_check failed: {check!r}")
            finally:
                destination_conn.close()
        finally:
            source_conn.close()

        os.replace(temp_path, destination_path)
        return destination_path
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    try:
        backup = backup_database(args.source, args.destination)
    except Exception as exc:
        print(f"SQLite backup failed: {exc}", file=sys.stderr)
        return 1
    print(f"SQLite backup verified: {backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
