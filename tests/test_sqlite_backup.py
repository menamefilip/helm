import sqlite3
from pathlib import Path

from scripts.backup_sqlite_db import backup_database


def test_online_backup_contains_committed_wal_rows(tmp_path):
    source = tmp_path / "live.db"
    destination = tmp_path / "backups" / "live.db.backup"
    writer = sqlite3.connect(source)
    try:
        mode = writer.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        assert mode.lower() == "wal"
        writer.execute("CREATE TABLE records (value TEXT NOT NULL)")
        writer.execute("INSERT INTO records VALUES ('committed in WAL')")
        writer.commit()
        wal_path = Path(f"{source}-wal")
        assert wal_path.exists() and wal_path.stat().st_size > 0

        backup_database(source, destination)

        with sqlite3.connect(destination) as backup:
            assert backup.execute("SELECT value FROM records").fetchall() == [("committed in WAL",)]
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        writer.close()
