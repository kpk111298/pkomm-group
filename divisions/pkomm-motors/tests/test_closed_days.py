"""
End-to-end tests for problem 001: a closed day stays closed.

The history is built once from nothing in a temporary folder, the way the
shipped warehouse is built. Each test then works on its own copy.

Run from divisions/pkomm-motors:  python -m pytest -q
"""

import os
import re
import shutil
import sys
from datetime import date, timedelta

import duckdb
import pytest

DIVISION = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, DIVISION)

from ingestion import ingest_bronze  # noqa: E402
from orchestration import nightly  # noqa: E402
from sources.dms import dms  # noqa: E402

LAST = dms.HISTORY_END
NEXT = LAST + timedelta(days=1)


def query(db, sql, params=None):
    con = duckdb.connect(db, read_only=True)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def closed_totals(db):
    return query(db, """
        SELECT g.sale_date, g.location_id, g.units_sold, ROUND(g.daily_revenue, 2)
        FROM main_gold.daily_sales_by_location g
        JOIN control.closed_days c ON c.business_date = g.sale_date
        ORDER BY 1, 2""")


@pytest.fixture(scope="module")
def history(tmp_path_factory):
    root = tmp_path_factory.mktemp("history")
    db = str(root / "warehouse.duckdb")
    out = nightly.build_history(db, str(root / "source"))
    return root, db, out


@pytest.fixture
def copy(history, tmp_path):
    root, db, _ = history
    target = str(tmp_path / "warehouse.duckdb")
    shutil.copy(db, target)
    shutil.copytree(root / "source", tmp_path / "source")
    return target, str(tmp_path / "source")


def test_history_builds_and_passes_every_test(history):
    _, db, out = history
    assert nightly.passed(out["results"])
    tests = [r for r in out["results"] if r["type"] == "test"]
    assert any(t["name"].startswith("closed_days_unchanged") for t in tests)
    # Every loaded day before the last one is closed; the last stays open
    days = query(db, "SELECT MIN(business_date), MAX(business_date), COUNT(*) FROM control.closed_days")[0]
    assert days[1] == LAST - timedelta(days=1)
    assert days[2] == (LAST - days[0]).days


def test_the_dealer_system_gives_the_same_day_every_time(tmp_path):
    a = dms.export_for(date(2026, 11, 10))
    b = dms.export_for(date(2026, 11, 10))
    for name in a:
        assert a[name].equals(b[name]), name
    # and it never writes a day twice
    dms.write_day(str(tmp_path), date(2026, 11, 10), a)
    with pytest.raises(dms.ClosedDayError):
        dms.write_day(str(tmp_path), date(2026, 11, 10), a)


def test_the_next_night_closes_yesterday_and_changes_no_closed_day(copy):
    db, source = copy
    before = closed_totals(db)
    out = nightly.run_day(db, source, NEXT)
    assert out["passed"]
    assert out["closed"] == [LAST]
    after = closed_totals(db)
    # Everything that was closed before is exactly the same, and the new closed day was added after it
    assert after[:len(before)] == before
    assert len(after) > len(before)


def test_rerunning_an_open_day_replaces_it_instead_of_doubling(copy):
    db, source = copy
    nightly.run_day(db, source, NEXT)
    counts = query(db, "SELECT COUNT(*) FROM bronze.sales_transactions")[0][0]
    out = nightly.run_day(db, source, NEXT)
    assert out["passed"]
    assert query(db, "SELECT COUNT(*) FROM bronze.sales_transactions")[0][0] == counts


def test_a_closed_day_cannot_be_loaded_again(copy):
    db, source = copy
    day = date(2026, 11, 10)
    before = query(db, "SELECT COUNT(*), SUM(sale_price) FROM bronze.sales_transactions WHERE _batch_date = ?", [day])
    con = duckdb.connect(db)
    try:
        with pytest.raises(ingest_bronze.ClosedDayError):
            ingest_bronze.load_day(con, day, os.path.join(source, f"day={day}"))
    finally:
        con.close()
    after = query(db, "SELECT COUNT(*), SUM(sale_price) FROM bronze.sales_transactions WHERE _batch_date = ?", [day])
    assert after == before


def test_a_changed_closed_day_stops_the_business_tables(copy):
    db, _ = copy
    # Something that bypasses the loader, like a hand-run fix, quietly drops a closed day's sale
    con = duckdb.connect(db)
    con.execute("""DELETE FROM bronze.sales_transactions WHERE transaction_id = (
                       SELECT MIN(transaction_id) FROM bronze.sales_transactions WHERE _batch_date = '2026-11-10')""")
    con.close()
    ok, results, _ = nightly.dbt("build", db)
    status = {r["name"]: r["status"] for r in results}
    check = next(n for n in status if n.startswith("closed_days_unchanged"))
    assert status[check] == "fail"
    assert status["daily_sales_by_location"] == "skipped"
    assert status["revenue_vs_target"] == "skipped"


def test_no_business_table_reads_the_servers_clock():
    models = os.path.join(nightly.DBT_DIR, "models")
    for folder, _, files in os.walk(models):
        for f in files:
            if f.endswith(".sql"):
                sql = re.sub(r"--.*", "", open(os.path.join(folder, f)).read())
                assert "CURRENT_DATE" not in sql.upper() and "NOW()" not in sql.upper(), f


def test_the_dealership_behaves_like_one(history):
    _, db, _ = history
    # A car is sold once
    assert query(db, """SELECT MAX(n) FROM (SELECT vehicle_id, COUNT(*) n
                        FROM main_silver.stg_sales_transactions GROUP BY 1)""")[0][0] == 1
    # No car sales on Sundays in Illinois or Texas
    assert query(db, """SELECT COUNT(*) FROM main_silver.stg_sales_transactions s
                        JOIN main_silver.stg_locations l USING (location_id)
                        WHERE l.state IN ('IL', 'TX') AND s.day_of_week = 0""")[0][0] == 0
    # Saturdays are the busiest day
    by_day = dict(query(db, """SELECT day_of_week, COUNT(*) FROM main_silver.stg_sales_transactions GROUP BY 1"""))
    assert max(by_day, key=by_day.get) == 6
