"""
Loads the dealer system's daily exports into the bronze layer.

Rules:
- Bronze only grows. Each day's rows are added with the day they belong to
  (_batch_date), when they landed (_ingested_at) and which file they came from.
- An open day can be loaded again. Its rows are replaced, never doubled.
- A closed day can never be loaded again. The loader refuses, and nothing changes.
- A day closes once the next day has been loaded and the build passed. Its
  totals are written to control.closed_day_totals at that moment, and a dbt
  test checks every build against them.

Run it from divisions/pkomm-motors:
    python -m ingestion.ingest_bronze            # loads every exported day not loaded yet
"""

from __future__ import annotations

import os
from datetime import date, datetime

import duckdb
import pandas as pd

DB_PATH = "pkomm_motors.duckdb"
SOURCE_ROOT = os.path.join("data", "source")

# Explicit types, so an empty day (a Sunday, the opening lot) never decides a column's type.
DDL = {
    "sales_transactions": """
        transaction_id VARCHAR, vehicle_id VARCHAR, employee_id VARCHAR, location_id VARCHAR,
        sale_price DOUBLE, sale_date TIMESTAMP, financing_approved BOOLEAN""",
    "service_jobs": """
        job_id VARCHAR, vehicle_id VARCHAR, technician_id VARCHAR, location_id VARCHAR,
        job_type VARCHAR, estimated_hours DOUBLE, actual_hours DOUBLE, labor_revenue DOUBLE,
        job_date TIMESTAMP""",
    "vehicles": """
        vehicle_id VARCHAR, make VARCHAR, model VARCHAR, year INTEGER, list_price DOUBLE,
        status VARCHAR, location_id VARCHAR, received_date DATE, updated_at TIMESTAMP""",
}
AUDIT = "_batch_date DATE, _ingested_at TIMESTAMP, _source_file VARCHAR"
DAILY_TABLES = list(DDL)


class ClosedDayError(Exception):
    """Raised when something tries to load a day that is already closed."""


def get_connection(path: str = DB_PATH):
    return duckdb.connect(path)


def setup(con) -> None:
    con.execute("CREATE SCHEMA IF NOT EXISTS bronze")
    con.execute("CREATE SCHEMA IF NOT EXISTS control")
    for table, cols in DDL.items():
        con.execute(f"CREATE TABLE IF NOT EXISTS bronze.{table} ({cols}, {AUDIT})")
    con.execute("""
        CREATE TABLE IF NOT EXISTS control.loads (
            batch_date DATE, table_name VARCHAR, rows_loaded BIGINT,
            loaded_at TIMESTAMP, source_file VARCHAR)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS control.closed_days (
            business_date DATE PRIMARY KEY, closed_at TIMESTAMP)""")
    con.execute("""
        CREATE TABLE IF NOT EXISTS control.closed_day_totals (
            business_date DATE, location_id VARCHAR,
            units_sold BIGINT, revenue DECIMAL(14, 2))""")


# Kept for older callers
create_bronze_schema = setup


def load_reference(con, folder: str) -> int:
    """Branches and staff. Reference data, so it is replaced as a whole."""
    total = 0
    for table in ("locations", "employees"):
        path = os.path.join(folder, f"{table}.csv")
        df = pd.read_csv(path)
        df["_ingested_at"] = datetime.now()
        df["_source_file"] = os.path.relpath(path, os.path.dirname(folder))
        con.execute(f"CREATE OR REPLACE TABLE bronze.{table} AS SELECT * FROM df")
        total += len(df)
    return total


def is_closed(con, day: date) -> bool:
    return con.execute("SELECT COUNT(*) FROM control.closed_days WHERE business_date = ?",
                       [day]).fetchone()[0] > 0


def loaded_days(con) -> list:
    return [r[0] for r in con.execute(
        "SELECT DISTINCT batch_date FROM control.loads ORDER BY 1").fetchall()]


def load_day(con, day: date, folder: str) -> dict:
    """Adds one day's export to bronze. Refuses a closed day."""
    if is_closed(con, day):
        raise ClosedDayError(f"{day:%B} {day.day}, {day.year} is closed, so its numbers can no longer change.")

    counts = {}
    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM control.loads WHERE batch_date = ?", [day])
        for table in DAILY_TABLES:
            path = os.path.join(folder, f"{table}.csv")
            source = os.path.join(os.path.basename(folder), f"{table}.csv")
            df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
            df["_batch_date"] = day
            df["_ingested_at"] = datetime.now()
            df["_source_file"] = source
            # Loading an open day again replaces it instead of adding a second copy
            con.execute(f"DELETE FROM bronze.{table} WHERE _batch_date = ?", [day])
            if len(df):
                con.execute(f"INSERT INTO bronze.{table} BY NAME SELECT * FROM df")
            con.execute("INSERT INTO control.loads VALUES (?, ?, ?, ?, ?)",
                        [day, table, len(df), datetime.now(), source])
            counts[table] = len(df)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return counts


def close_days_before(con, day: date) -> list:
    """
    Closes every loaded day before `day` that is still open, and records its
    totals as the gold layer shows them right now. Call only after a build passed.
    """
    open_days = [r[0] for r in con.execute("""
        SELECT DISTINCT batch_date FROM control.loads
        WHERE batch_date < ? AND batch_date NOT IN (SELECT business_date FROM control.closed_days)
        ORDER BY 1""", [day]).fetchall()]
    if not open_days:
        return []
    con.execute("BEGIN TRANSACTION")
    try:
        for d in open_days:
            con.execute("""
                INSERT INTO control.closed_day_totals
                SELECT sale_date, location_id, units_sold, CAST(daily_revenue AS DECIMAL(14, 2))
                FROM main_gold.daily_sales_by_location WHERE sale_date = ?""", [d])
            con.execute("INSERT INTO control.closed_days VALUES (?, ?)", [d, datetime.now()])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return open_days


def pending_days(con, root: str = SOURCE_ROOT) -> list:
    """Exported days that are not in bronze yet, oldest first."""
    done = set(loaded_days(con))
    days = []
    for name in sorted(os.listdir(root)):
        if name.startswith("day="):
            d = date.fromisoformat(name[4:])
            if d not in done:
                days.append((d, os.path.join(root, name)))
    return days


def main():
    con = get_connection()
    setup(con)
    load_reference(con, os.path.join(SOURCE_ROOT, "reference"))
    for d, folder in pending_days(con):
        counts = load_day(con, d, folder)
        print(f"Loaded {d}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    con.close()


if __name__ == "__main__":
    main()
