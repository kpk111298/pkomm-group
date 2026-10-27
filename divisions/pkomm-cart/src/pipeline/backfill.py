from __future__ import annotations

import os
import subprocess
import sys
from datetime import timedelta

from src.utils.date_helpers import parse_yyyy_mm_dd, daterange


def _run(module: str, **env: str) -> None:
    """Run one step as its own process, with the same Python that started this one."""
    cmd = [sys.executable, "-m", module]
    rc = subprocess.run(cmd, env={**os.environ, **env}).returncode
    if rc != 0:
        shown = " ".join(f"{k}={v}" for k, v in env.items())
        raise SystemExit(f"Command failed: {shown} python -m {module}")


def main() -> None:
    start = os.getenv("START_DATE", "").strip()
    end = os.getenv("END_DATE", "").strip()
    if not start or not end:
        raise SystemExit("Use: START_DATE=2026-01-01 END_DATE=2026-01-25 python -m src.pipeline.backfill")

    start_d = parse_yyyy_mm_dd(start)
    end_d = parse_yyyy_mm_dd(end)

    # Ensure Day 1 exists as a fresh day (no prev required)
    # Our generator automatically uses "fresh day" behavior if prev day folder is missing.

    for d in daterange(start_d, end_d):
        ds = d.isoformat()
        prev = (d - timedelta(days=1)).isoformat()
        _run("src.pipeline.run_daily", RUN_DATE=ds, PREV_DATE=prev)

    print(f"Backfill complete: {start} -> {end}")


if __name__ == "__main__":
    main()
