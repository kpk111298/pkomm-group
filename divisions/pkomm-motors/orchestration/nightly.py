"""
The Pkomm Motors daily run, the way a scheduler would run it every night:

1. The dealer system exports the business day.
2. The loader adds that day to bronze (and refuses any closed day).
3. dbt build cleans, tests and models. A failed test stops the business tables.
4. If the build passed, every earlier day closes, and its totals are recorded.

Run it from divisions/pkomm-motors:
    python -m orchestration.nightly history             # the standard history, from scratch
    python -m orchestration.nightly day 2026-11-14      # one more day
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import date, timedelta

import duckdb

DIVISION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIVISION)

from ingestion import ingest_bronze  # noqa: E402
from sources.dms import dms  # noqa: E402

DBT_DIR = os.path.join(DIVISION, "dbt_project", "pkomm_motors")
GOLD = {f[:-4] for f in os.listdir(os.path.join(DBT_DIR, "models", "marts")) if f.endswith(".sql")}


def dbt(command: str, db_path: str, workdir: str | None = None):
    """Runs dbt in its own process and reads run_results.json for what happened."""
    workdir = workdir or tempfile.mkdtemp(prefix="pkomm_dbt_")
    target = os.path.join(workdir, "target")
    args = [
        sys.executable, "-c", "from dbt.cli.main import cli; cli()",
        command,
        "--project-dir", DBT_DIR,
        "--profiles-dir", DBT_DIR,
        "--target-path", target,
        "--log-path", os.path.join(workdir, "logs"),
        "--no-use-colors",
    ]
    env = dict(os.environ, PKOMM_MOTORS_DB=os.path.abspath(db_path), DBT_SEND_ANONYMOUS_USAGE_STATS="false")
    proc = subprocess.run(args, env=env, capture_output=True, text=True, timeout=600)
    results = []
    path = os.path.join(target, "run_results.json")
    if os.path.exists(path):
        with open(path) as f:
            for r in json.load(f)["results"]:
                results.append({
                    "type": r["unique_id"].split(".")[0],
                    "name": r["unique_id"].split(".")[2],
                    "status": r["status"],
                    "seconds": round(r.get("execution_time") or 0, 2),
                    "failures": r.get("failures") or 0,
                })
    return proc.returncode == 0, results, proc.stdout[-4000:]


def passed(results) -> bool:
    gold_ok = all(r["status"] == "success" for r in results if r["type"] == "model" and r["name"] in GOLD)
    tests_ok = all(r["status"] == "pass" for r in results if r["type"] == "test")
    built = {r["name"] for r in results if r["type"] == "model"}
    return gold_ok and tests_ok and GOLD <= built


def build_history(db_path: str, source_root: str, through: date = dms.HISTORY_END) -> dict:
    """Builds a warehouse from nothing: every day up to `through` exported, loaded and
    built, and every day before `through` closed. `through` stays open."""
    dms.write_history(source_root, through)
    con = duckdb.connect(db_path)
    ingest_bronze.setup(con)
    ingest_bronze.load_reference(con, os.path.join(source_root, "reference"))
    for d, folder in ingest_bronze.pending_days(con, source_root):
        ingest_bronze.load_day(con, d, folder)
    con.close()

    ok, results, log = dbt("build", db_path)
    if not passed(results):
        raise RuntimeError("The history build did not pass:\n" + log)

    con = duckdb.connect(db_path)
    closed = ingest_bronze.close_days_before(con, through)
    con.close()
    return {"closed": len(closed), "results": results}


def run_day(db_path: str, source_root: str, day: date, export: dict | None = None,
            workdir: str | None = None) -> dict:
    """One nightly run for `day`."""
    folder = os.path.join(source_root, f"day={day.isoformat()}")
    if not os.path.exists(folder):
        dms.write_day(source_root, day, export or dms.export_for(day))

    con = duckdb.connect(db_path)
    ingest_bronze.setup(con)
    counts = ingest_bronze.load_day(con, day, folder)  # raises ClosedDayError for a closed day
    con.close()

    ok, results, log = dbt("build", db_path, workdir)
    closed = []
    if passed(results):
        con = duckdb.connect(db_path)
        closed = ingest_bronze.close_days_before(con, day)
        con.close()
    return {"loaded": counts, "results": results, "closed": closed, "passed": passed(results), "log": log}


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    db_path = os.path.join(DIVISION, ingest_bronze.DB_PATH)
    source_root = os.path.join(DIVISION, dms.SOURCE_ROOT)
    if argv[:1] == ["history"]:
        if os.path.exists(db_path):
            os.remove(db_path)
        out = build_history(db_path, source_root)
        print(f"Built the history through {dms.HISTORY_END}. {out['closed']} days closed.")
    elif argv[:1] == ["day"] and len(argv) == 2:
        day = date.fromisoformat(argv[1])
        out = run_day(db_path, source_root, day)
        status = "passed" if out["passed"] else "held back"
        print(f"{day}: loaded {out['loaded']}, build {status}, closed {len(out['closed'])} day(s)")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
