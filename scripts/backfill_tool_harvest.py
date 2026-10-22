#!/usr/bin/env python3
"""Safely backfill tool activity and optional summaries from local runtime data.

The default mode is a dry run: it operates on a temporary database copy so
commits performed by the harvest helper cannot modify the original database.
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import connect
from app.main import (
    DEFAULT_DB,
    _auto_run_summary,
    _harvest_runtime_tool_events,
    _runtime_state_db_path,
)


def _tool_count(conn, run_id: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM run_events WHERE run_id = ? AND type = 'tool_activity'",
        (run_id,),
    ).fetchone()[0]


def _has_summary(conn, run_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM project_run_summaries WHERE run_id = ?", (run_id,)
    ).fetchone() is not None


def _stdout_for(artifact_path: str) -> str:
    """Read local run output when a missing summary was explicitly requested."""
    for name in ("runtime-stdout.log", "output.md"):
        candidate = Path(artifact_path) / name
        if candidate.exists() and candidate.is_file():
            try:
                return candidate.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes (default is dry-run)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="workspace database path")
    parser.add_argument("--summaries", action="store_true", help="also create missing run summaries")
    args = parser.parse_args()

    runtime_db = _runtime_state_db_path()
    if runtime_db is None or not Path(runtime_db).is_file():
        print(json.dumps({"error": "configured runtime database is unavailable"}))
        return 1

    db_path = Path(args.db).expanduser()
    if not db_path.is_file():
        print(json.dumps({"error": "workspace database is unavailable"}))
        return 1

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if args.apply:
        target_db = db_path
    else:
        temp_dir = tempfile.TemporaryDirectory()
        target_db = Path(temp_dir.name) / "dry-run.db"
        shutil.copy2(db_path, target_db)

    report: list[dict[str, object]] = []
    totals = {"runs": 0, "tools_before": 0, "tools_after": 0, "summaries_created": 0}
    try:
        with connect(target_db) as conn:
            runs = [
                dict(row)
                for row in conn.execute(
                    "SELECT id, status, prompt, artifact_path, chat_id FROM runs "
                    "WHERE artifact_path IS NOT NULL AND artifact_path != '' ORDER BY id"
                )
            ]
            for run in runs:
                run_id = int(run["id"])
                artifact_path = str(run["artifact_path"])
                if not Path(artifact_path).is_dir():
                    continue

                before = _tool_count(conn, run_id)
                _harvest_runtime_tool_events(conn, run_id, artifact_path)
                after = _tool_count(conn, run_id)

                summary_created = False
                if args.summaries and not _has_summary(conn, run_id):
                    stdout = _stdout_for(artifact_path)
                    if stdout.strip():
                        _auto_run_summary(
                            conn,
                            run,
                            {"stdout": stdout, "stderr": "", "exit_code": 0, "timed_out": False},
                        )
                        summary_created = _has_summary(conn, run_id)

                totals["runs"] += 1
                totals["tools_before"] += before
                totals["tools_after"] += after
                totals["summaries_created"] += int(summary_created)
                if after != before or summary_created:
                    report.append({
                        "run": run_id,
                        "tools_before": before,
                        "tools_after": after,
                        "summary_created": summary_created,
                    })
            conn.commit()
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "totals": totals, "changed": report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
