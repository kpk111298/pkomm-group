"""
Runs one night of the Pkomm Motors pipeline on demand, for the "How it runs" tab.

The dashboard ships with a warehouse that holds the business from August 15 to
November 13, 2026. Every day before November 13 is closed. A run copies that
warehouse into its own folder in /tmp and does what the nightly job does:

    the dealer system exports the next business day (November 14),
    the loader adds it to the raw layer,
    dbt build cleans, tests and models it,
    and if the build passed, November 13 closes.

Break it plants bad records into that new day before it loads.
Rewrite the past makes the dealer system resend a closed day with different
numbers, which the loader refuses.

Nothing is saved, and visitors never touch each other's data.
"""

import os
import random
import shutil
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, timedelta

import duckdb
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DIVISION = os.path.dirname(HERE)
sys.path.insert(0, DIVISION)

from ingestion import ingest_bronze  # noqa: E402
from orchestration import nightly  # noqa: E402
from sources.dms import dms  # noqa: E402

SAMPLE_DB = os.path.join(DIVISION, "pkomm_motors.duckdb")
DBT_DIR = nightly.DBT_DIR

# Business tables live in models/marts. If a test on anything upstream fails,
# dbt build skips them, so bad data never reaches what the business reads.
GOLD = nightly.GOLD

# The closed day that Rewrite the past tries to change, and the branch it picks on
REWRITE_DAY = date(2026, 11, 10)
REWRITE_BRANCH = ("LOC002", "Chicago")

# The free server is small. One run at a time keeps memory in check.
_run_lock = threading.Lock()


class PipelineBusy(Exception):
    pass


@dataclass
class Step:
    name: str
    detail: str = ""
    seconds: float = 0.0
    ok: bool = True
    state: str = ""  # overrides Done or Failed, e.g. "Refused"


@dataclass
class RunResult:
    workdir: str
    db_path: str
    seed: int
    broke_it: bool
    mode: str = "run"          # run, break or rewrite
    day: date = None           # the business day this run worked on
    steps: list = field(default_factory=list)
    planted: dict = field(default_factory=dict)
    tests: list = field(default_factory=list)
    health: list = field(default_factory=list)
    closed: dict = field(default_factory=dict)   # closed days before and after the run
    rewrite: dict = field(default_factory=dict)  # what Rewrite the past tried and what happened
    finished_at: float = 0.0
    published: bool = False

    @property
    def tests_failed(self):
        return [t for t in self.tests if t["status"] in ("fail", "error")]

    @property
    def tests_skipped(self):
        return [t for t in self.tests if t["status"] == "skipped"]


# ------------------------------------------------------------
# Break it: plant known problems in the raw files
# ------------------------------------------------------------

def plant_problems(sales, vehicles, rng):
    """Adds the kind of mess real source systems send. Returns the counts."""
    dupes = sales.sample(6, random_state=rng.randint(0, 9999))
    sales = pd.concat([sales, dupes], ignore_index=True)

    neg = sales.sample(4, random_state=rng.randint(0, 9999)).index
    sales.loc[neg, "sale_price"] = -sales.loc[neg, "sale_price"]

    missing = sales.drop(neg).sample(3, random_state=rng.randint(0, 9999)).index
    sales.loc[missing, "employee_id"] = None

    odd = vehicles.sample(2, random_state=rng.randint(0, 9999)).index
    vehicles.loc[odd, "status"] = "SOLD - PENDING"

    planted = {
        "duplicate_sales": 6,
        "negative_prices": 4,
        "missing_employee": 3,
        "bad_vehicle_status": 2,
    }
    return sales, vehicles, planted


# ------------------------------------------------------------
# Data health: what was wrong in raw, and what happened to it
# ------------------------------------------------------------

HEALTH_CHECKS = [
    {
        "key": "duplicate_sales",
        "label": "Duplicate sales",
        "raw": """SELECT COUNT(*) - COUNT(DISTINCT transaction_id)
                  FROM bronze.sales_transactions""",
        "clean": """SELECT COUNT(*) - COUNT(DISTINCT transaction_id)
                    FROM main_silver.stg_sales_transactions""",
        "test": "unique_stg_sales_transactions_transaction_id",
    },
    {
        "key": "negative_prices",
        "label": "Zero or negative prices",
        "raw": "SELECT COUNT(*) FROM bronze.sales_transactions WHERE sale_price <= 0",
        "clean": "SELECT COUNT(*) FROM main_silver.stg_sales_transactions WHERE sale_price <= 0",
        "test": None,
    },
    {
        "key": "missing_employee",
        "label": "Sales with no salesperson",
        "raw": "SELECT COUNT(*) FROM bronze.sales_transactions WHERE employee_id IS NULL",
        "clean": "SELECT COUNT(*) FROM main_silver.stg_sales_transactions WHERE employee_id IS NULL",
        "test": None,
    },
    {
        "key": "bad_vehicle_status",
        "label": "Unknown vehicle status",
        "raw": """SELECT COUNT(*) FROM bronze.vehicles
                  WHERE LOWER(status) NOT IN ('available', 'sold', 'reserved')""",
        "clean": """SELECT COUNT(*) FROM main_silver.stg_vehicles
                    WHERE status NOT IN ('available', 'sold', 'reserved')""",
        "test": "accepted_values_stg_vehicles_status__available__sold__reserved",
    },
]


def check_health(db_path, tests):
    failed_tests = {t["name"] for t in tests if t["status"] != "pass"}
    con = duckdb.connect(db_path, read_only=True)
    rows = []
    for c in HEALTH_CHECKS:
        found = con.execute(c["raw"]).fetchone()[0]
        left = con.execute(c["clean"]).fetchone()[0]
        if found == 0:
            outcome = "None found"
        elif left == 0:
            outcome = "Removed by the cleaning step"
        elif c["test"] and c["test"] in failed_tests:
            outcome = "Got through cleaning, stopped by a test"
        else:
            outcome = "Got through unnoticed"
        rows.append({"key": c["key"], "label": c["label"], "found": int(found),
                     "left": int(left), "outcome": outcome})
    con.close()
    return rows


def table_counts(db_path, schema):
    con = duckdb.connect(db_path, read_only=True)
    rows = con.execute(f"""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = '{schema}' ORDER BY table_name
    """).fetchall()
    counts = {t: con.execute(f'SELECT COUNT(*) FROM {schema}."{t}"').fetchone()[0]
              for (t,) in rows}
    con.close()
    return counts


# ------------------------------------------------------------
# Closed days: their numbers before and after a run
# ------------------------------------------------------------

def closed_snapshot(db_path):
    """Units and revenue per branch for every closed day, as the gold table shows them."""
    con = duckdb.connect(db_path, read_only=True)
    try:
        rows = con.execute("""
            SELECT g.sale_date, g.location_id, g.units_sold, ROUND(g.daily_revenue, 2)
            FROM main_gold.daily_sales_by_location g
            JOIN control.closed_days c ON c.business_date = g.sale_date
            ORDER BY 1, 2""").fetchall()
        days = con.execute("SELECT COUNT(*) FROM control.closed_days").fetchone()[0]
    finally:
        con.close()
    return {"days": days, "rows": rows}


def next_business_day(db_path):
    con = duckdb.connect(db_path, read_only=True)
    try:
        last = con.execute("SELECT MAX(batch_date) FROM control.loads").fetchone()[0]
    finally:
        con.close()
    return last + timedelta(days=1)


def units_on(db_path, day, location_id):
    con = duckdb.connect(db_path, read_only=True)
    try:
        row = con.execute("""SELECT units_sold, ROUND(daily_revenue, 2) FROM main_gold.daily_sales_by_location
                             WHERE sale_date = ? AND location_id = ?""", [day, location_id]).fetchone()
    finally:
        con.close()
    return (int(row[0]), float(row[1])) if row else (0, 0.0)


# ------------------------------------------------------------
# The run itself
# ------------------------------------------------------------

def _fmt(day):
    return day.strftime("%A, %B ") + str(day.day)


def run(seed=None, break_it=False, rewrite=False, on_step=None):
    """One night's run, Break it, or Rewrite the past. Calls on_step(Step) as it goes."""
    if not _run_lock.acquire(blocking=False):
        raise PipelineBusy()

    seed = seed if seed is not None else random.randint(1, 99999)
    workdir = tempfile.mkdtemp(prefix="pkomm_motors_")
    db_path = os.path.join(workdir, "pkomm_motors.duckdb")
    source_root = os.path.join(workdir, "source")
    mode = "rewrite" if rewrite else ("break" if break_it else "run")
    result = RunResult(workdir=workdir, db_path=db_path, seed=seed, broke_it=break_it and not rewrite, mode=mode)

    def record(step):
        result.steps.append(step)
        if on_step:
            on_step(step)

    try:
        shutil.copy(SAMPLE_DB, db_path)
        before = closed_snapshot(db_path)
        result.closed["before_days"] = before["days"]

        if rewrite:
            _rewrite_the_past(result, source_root, before, record)
        else:
            _one_night(result, source_root, before, record)
    finally:
        _run_lock.release()

    result.finished_at = time.time()
    return result


def _one_night(result, source_root, before, record):
    db_path = result.db_path
    day = next_business_day(db_path)
    result.day = day

    # 1. The dealer system exports the day. Same day, same numbers, every time.
    t0 = time.time()
    export = dms.export_for(day)
    sales, vehicles = export["sales_transactions"], export["vehicles"]
    record(Step("Export the day",
                f"{_fmt(day)}: {len(sales)} cars sold, {len(export['service_jobs'])} service jobs, "
                f"{len(vehicles)} lot changes", time.time() - t0))

    if result.broke_it:
        t0 = time.time()
        sales, vehicles, result.planted = plant_problems(sales, vehicles, random.Random(result.seed))
        export = dict(export, sales_transactions=sales, vehicles=vehicles)
        record(Step("Plant problems",
                    f"{sum(result.planted.values())} bad records slipped into {_fmt(day)}, seed {result.seed}",
                    time.time() - t0))

    # 2. Add the day to the raw layer. Closed days are not touched.
    t0 = time.time()
    folder = dms.write_day(source_root, day, export)
    con = duckdb.connect(db_path)
    counts = ingest_bronze.load_day(con, day, folder)
    con.close()
    record(Step("Load raw layer",
                f"{sum(counts.values()):,} rows added for {day:%B} {day.day}. "
                f"The {before['days']} closed days before it were not touched", time.time() - t0))

    # 3. One dbt build: each model, then its tests, then whatever depends on it.
    t0 = time.time()
    _, results, _ = nightly.dbt("build", db_path, result.workdir)
    wall = time.time() - t0
    models = [r for r in results if r["type"] == "model" and r["name"] not in GOLD]
    gold = [r for r in results if r["type"] == "model" and r["name"] in GOLD]
    tests = [r for r in results if r["type"] == "test"]
    result.tests = tests

    t_tests = sum(t["seconds"] for t in tests)
    t_gold = sum(g["seconds"] for g in gold)
    built = sum(1 for m in models if m["status"] == "success")
    record(Step("Clean and model with dbt", f"{built} of {len(models)} cleaned tables built",
                max(wall - t_tests - t_gold, 0), built == len(models)))

    passed = sum(1 for t in tests if t["status"] == "pass")
    failed = len(result.tests_failed)
    skipped = len(result.tests_skipped)
    if failed:
        detail = f"{failed} failed, {passed} passed"
        if skipped:
            detail += f", {skipped} skipped with their held-back tables"
    else:
        detail = f"{passed} of {len(tests)} tests passed, including the closed-days check"
    record(Step("Run data tests", detail, t_tests, failed == 0))

    # 4. What the business sees, and closing yesterday
    t0 = time.time()
    result.health = check_health(db_path, tests)
    ready = sum(1 for g in gold if g["status"] == "success")
    result.published = ready == len(GOLD) and failed == 0
    if result.published:
        con = duckdb.connect(db_path)
        newly = ingest_bronze.close_days_before(con, day)
        con.close()
        after = closed_snapshot(db_path)
        unchanged = after["rows"][:len(before["rows"])] == before["rows"]
        result.closed.update(after_days=after["days"], newly=newly, unchanged=unchanged)
        closed_txt = ", ".join(f"{d:%B} {d.day}" for d in newly) or "nothing new"
        detail = (f"{ready} tables ready. Closed {closed_txt}. "
                  f"{before['days']} earlier closed days checked: "
                  f"{'none changed' if unchanged else 'SOME CHANGED'}")
    else:
        result.closed.update(after_days=before["days"], newly=[], unchanged=True)
        detail = f"Held back: {len(GOLD) - ready} of {len(GOLD)} not rebuilt because tests failed upstream"
    record(Step("Build business tables", detail, t_gold + time.time() - t0, result.published))


def _rewrite_the_past(result, source_root, before, record):
    """The old problem, on purpose: the dealer system resends a closed day with different numbers."""
    db_path = result.db_path
    day = REWRITE_DAY
    loc_id, city = REWRITE_BRANCH
    result.day = day
    units_before, revenue_before = units_on(db_path, day, loc_id)

    t0 = time.time()
    export = dms.export_for(day)
    sales = export["sales_transactions"]
    branch = sales[sales.location_id == loc_id]
    # The resend loses one of the branch's sales, the way a rebuilt history quietly changes
    resent = sales.drop(branch.index[-1:]) if len(branch) else sales
    export = dict(export, sales_transactions=resent)
    units_resent = int((resent.location_id == loc_id).sum())
    record(Step("Dealer system resends a closed day",
                f"{_fmt(day)} sent again. It now says {city} sold {units_resent} "
                f"{'car' if units_resent == 1 else 'cars'}, not {units_before}", time.time() - t0))

    t0 = time.time()
    folder = os.path.join(source_root, "resend", f"day={day.isoformat()}")
    os.makedirs(folder)
    for name, df in export.items():
        df.to_csv(os.path.join(folder, f"{name}.csv"), index=False)
    con = duckdb.connect(db_path)
    try:
        ingest_bronze.load_day(con, day, folder)
        refused, why = False, "The loader accepted it"
    except ingest_bronze.ClosedDayError as e:
        refused, why = True, str(e)
    finally:
        con.close()
    record(Step("Load raw layer", why, time.time() - t0, refused, "Refused" if refused else "Accepted"))

    t0 = time.time()
    units_after, revenue_after = units_on(db_path, day, loc_id)
    after = closed_snapshot(db_path)
    record(Step("Check the closed day",
                f"{city}, {day:%B} {day.day}: {units_before} before, {units_after} after. "
                f"All {after['days']} closed days {'unchanged' if after['rows'] == before['rows'] else 'CHANGED'}", time.time() - t0, units_after == units_before))
    result.rewrite = {"day": day, "city": city, "before": units_before, "resent": units_resent,
                      "after": units_after, "revenue": revenue_before, "refused": refused}
    result.closed.update(after_days=after["days"], newly=[], unchanged=after["rows"] == before["rows"])


def cleanup(result):
    if result and result.workdir.startswith(tempfile.gettempdir()):
        shutil.rmtree(result.workdir, ignore_errors=True)
