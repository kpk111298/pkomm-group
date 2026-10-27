from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta

import duckdb

from src.utils.date_helpers import parse_yyyy_mm_dd


def _run(module: str, **env: str) -> None:
    """Run one step as its own process, with the same Python that started this one."""
    cmd = [sys.executable, "-m", module]
    rc = subprocess.run(cmd, env={**os.environ, **env}).returncode
    if rc != 0:
        shown = " ".join(f"{k}={v}" for k, v in env.items())
        raise SystemExit(f"Command failed: {shown} python -m {module}")


def _record_pipeline_run(run_date: str, status: str, message: str = "") -> None:
    con = duckdb.connect("warehouse/ecomm.duckdb")
    con.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id BIGINT,
            run_date VARCHAR,
            run_ts TIMESTAMP,
            status VARCHAR,
            message VARCHAR
        );
    """)
    run_id = int(datetime.now().timestamp())
    con.execute(
        "INSERT INTO pipeline_runs VALUES (?, ?, ?, ?, ?)",
        [run_id, run_date, datetime.now(), status, message],
    )
    con.close()


def main() -> None:
    run_date_str = os.getenv("RUN_DATE", "").strip()
    if not run_date_str:
        raise SystemExit("Set RUN_DATE like: RUN_DATE=2026-01-26 python -m src.pipeline.run_daily")

    run_date = parse_yyyy_mm_dd(run_date_str)
    prev_date_str = os.getenv("PREV_DATE", "").strip()
    if not prev_date_str:
        prev_date_str = (run_date - timedelta(days=1)).isoformat()

    try:
        # 1) generate source (incremental day uses prev day)
        _run("src.ingestion.generate_source_data", RUN_DATE=run_date_str, PREV_DATE=prev_date_str)

        # 2) bronze load
        _run("src.ingestion.load_bronze", RUN_DATE=run_date_str)

        # 3) silver build for this batch
        _run("src.transformations.build_silver", BATCH_DATE=run_date_str)

        # 4) gold rebuild from silver, plus a snapshot of the KPIs as of this run
        _run("src.models.build_gold", RUN_DATE=run_date_str)

        _record_pipeline_run(run_date_str, "SUCCESS", f"Completed pipeline for {run_date_str}")
        print(f"Pipeline complete for {run_date_str}")

    except SystemExit as e:
        _record_pipeline_run(run_date_str, "FAILED", str(e))
        raise


if __name__ == "__main__":
    main()
