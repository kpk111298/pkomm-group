"""
End-to-end tests for the Pkomm Cart pipeline.

Each test session copies the project into a temporary folder, runs five days
from an empty warehouse, and checks what a data engineer would check before
trusting the numbers: a fresh start works, late updates land, nothing doubles,
and a rerun changes nothing.

Run from divisions/pkomm-cart:  python -m pytest -q
"""

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

PROJECT = Path(__file__).resolve().parent.parent
DAYS = ["2026-01-26", "2026-01-27", "2026-01-28", "2026-01-29", "2026-01-30"]
UPSERT_KEYS = {
    "customers": "customer_id",
    "products": "product_id",
    "orders": "order_id",
    "payments": "payment_id",
    "shipments": "shipment_id",
}


def run_day(workdir: Path, day: str, prev: str) -> None:
    env = {"RUN_DATE": day, "PREV_DATE": prev, "PATH": "/usr/bin:/bin"}
    result = subprocess.run(
        [sys.executable, "-m", "src.pipeline.run_daily"],
        cwd=workdir, env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def query(workdir: Path, sql: str):
    con = duckdb.connect(str(workdir / "warehouse" / "ecomm.duckdb"), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def table_counts(workdir: Path) -> dict:
    names = [r[0] for r in query(workdir, """
        SELECT table_name FROM information_schema.tables
        WHERE table_name LIKE 'bronze_%' OR table_name LIKE 'silver_%' OR table_name LIKE 'gold_%'
        ORDER BY table_name
    """)]
    return {n: query(workdir, f"SELECT COUNT(*) FROM {n}")[0][0] for n in names}


def file_hashes(folder: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.glob("*.csv"))}


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    """Five days from an empty warehouse, with snapshots taken along the way."""
    workdir = tmp_path_factory.mktemp("pkomm_cart")
    shutil.copytree(PROJECT / "src", workdir / "src")
    (workdir / "warehouse").mkdir()
    (workdir / "data" / "source").mkdir(parents=True)

    snaps = {}
    prev = "2026-01-25"
    for day in DAYS:
        run_day(workdir, day, prev)
        snaps[day] = {
            "kpi": dict(query(workdir, "SELECT CAST(order_date AS VARCHAR), captured_revenue FROM gold_kpi_daily")),
            "gross": dict(query(workdir, "SELECT CAST(order_date AS VARCHAR), gross_revenue FROM gold_kpi_daily")),
        }
        prev = day
    return workdir, snaps


def test_fresh_start_builds_every_layer(runs):
    workdir, _ = runs
    counts = table_counts(workdir)
    for layer in ("bronze_orders", "silver_orders", "gold_fact_orders", "gold_kpi_daily"):
        assert counts.get(layer, 0) > 0, f"{layer} is empty"
    assert counts["gold_kpi_daily"] == len(DAYS)


def test_each_day_is_different_and_replays_the_same(runs, tmp_path):
    workdir, _ = runs
    orders_per_day = query(workdir, "SELECT CAST(order_date AS VARCHAR), orders_total FROM gold_kpi_daily ORDER BY 1")
    assert len({n for _, n in orders_per_day}) > 1, f"every day has the same order count: {orders_per_day}"

    # Generating the same day again gives byte-for-byte the same files
    day_dir = workdir / "data" / "source" / f"day={DAYS[0]}"
    before = file_hashes(day_dir)
    replay = tmp_path / "replay"
    shutil.copytree(PROJECT / "src", replay / "src")
    (replay / "data" / "source").mkdir(parents=True)
    result = subprocess.run(
        [sys.executable, "-m", "src.ingestion.generate_source_data"],
        cwd=replay, env={"RUN_DATE": DAYS[0], "PREV_DATE": "2026-01-25", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert file_hashes(replay / "data" / "source" / f"day={DAYS[0]}") == before


def test_every_day_has_revenue_and_every_order_has_items(runs):
    workdir, _ = runs
    days = query(workdir, "SELECT CAST(order_date AS VARCHAR), gross_revenue FROM gold_kpi_daily ORDER BY 1")
    assert all(rev > 0 for _, rev in days), f"a day with no revenue: {days}"
    empty = query(workdir, "SELECT COUNT(*) FROM gold_fact_orders WHERE line_count = 0")[0][0]
    assert empty == 0, f"{empty} orders have no line items"


def test_source_ids_are_never_reused(runs):
    workdir, _ = runs
    # A source system never hands out the same shipment or payment ID twice for different orders
    for table, key in (("shipments", "shipment_id"), ("payments", "payment_id"), ("orders", "order_id")):
        clashes = query(workdir, f"""
            SELECT COUNT(*) FROM (
                SELECT {key} FROM bronze_{table} GROUP BY {key} HAVING COUNT(DISTINCT order_id) > 1
            )
        """)[0][0]
        assert clashes == 0, f"{clashes} {key}s point at more than one order"


def test_silver_keeps_one_row_per_key(runs):
    workdir, _ = runs
    for table, key in UPSERT_KEYS.items():
        total, distinct = query(workdir, f"SELECT COUNT(*), COUNT(DISTINCT {key}) FROM silver_{table}")[0]
        assert total == distinct, f"silver_{table} has {total - distinct} duplicate {key}s"


def test_silver_holds_the_newest_version(runs):
    workdir, _ = runs
    for table, key in UPSERT_KEYS.items():
        stale = query(workdir, f"""
            WITH newest AS (
                SELECT {key}, MAX(CAST(updated_at AS TIMESTAMP)) AS updated_at
                FROM bronze_{table} GROUP BY {key}
            )
            SELECT COUNT(*) FROM newest n
            JOIN silver_{table} s ON s.{key} = n.{key}
            WHERE CAST(s.updated_at AS TIMESTAMP) <> n.updated_at
        """)[0][0]
        assert stale == 0, f"silver_{table} has {stale} rows that are not the newest version"


def test_late_payments_change_past_revenue_correctly(runs):
    workdir, snaps = runs
    day1 = DAYS[0]
    # Nobody places a Jan 26 order on Jan 27, so gross revenue for Jan 26 must not move
    assert snaps[DAYS[1]]["gross"][day1] == snaps[day1]["gross"][day1]
    # but late payments land, so collected revenue for Jan 26 does
    assert snaps[DAYS[1]]["kpi"][day1] != snaps[day1]["kpi"][day1]

    # And it must match an independent recount straight from the raw payments
    expected = query(workdir, f"""
        WITH latest_payment AS (
            SELECT order_id, MAX_BY(payment_status, CAST(updated_at AS TIMESTAMP)) AS status
            FROM bronze_payments GROUP BY order_id
        ),
        order_totals AS (
            SELECT i.order_id, SUM(i.line_amount) AS amount
            FROM (SELECT DISTINCT ON (order_item_id) * FROM bronze_order_items) i
            GROUP BY i.order_id
        )
        SELECT COALESCE(SUM(t.amount), 0)
        FROM (SELECT DISTINCT order_id, CAST(created_at AS DATE) AS d FROM bronze_orders) o
        JOIN latest_payment p ON p.order_id = o.order_id
        JOIN order_totals t ON t.order_id = o.order_id
        WHERE o.d = DATE '{day1}' AND p.status = 'CAPTURED'
    """)[0][0]
    actual = snaps[DAYS[-1]]["kpi"][day1]
    assert float(actual) == pytest.approx(float(expected), abs=0.01)


def test_rerunning_a_day_changes_nothing(runs):
    workdir, _ = runs
    before_counts = table_counts(workdir)
    before_kpi = query(workdir, "SELECT * FROM gold_kpi_daily ORDER BY order_date")

    run_day(workdir, DAYS[-1], DAYS[-2])

    assert table_counts(workdir) == before_counts
    assert query(workdir, "SELECT * FROM gold_kpi_daily ORDER BY order_date") == before_kpi
