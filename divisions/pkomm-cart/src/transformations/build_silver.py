from __future__ import annotations

import os
from datetime import datetime
from typing import Dict, Tuple

import duckdb


UPSERT_TABLES: Dict[str, Tuple[str, str]] = {
    "customers": ("customer_id", "updated_at"),
    "products": ("product_id", "updated_at"),
    "orders": ("order_id", "updated_at"),
    "payments": ("payment_id", "updated_at"),
    "shipments": ("shipment_id", "updated_at"),
}

APPEND_ONLY_TABLES: Dict[str, str] = {
    "order_items": "order_item_id",
}


def ensure_silver_table(con: duckdb.DuckDBPyConnection, table: str) -> None:
    con.execute(f"CREATE TABLE IF NOT EXISTS silver_{table} AS SELECT * FROM bronze_{table} WHERE 1=0;")


def build_silver_upsert(con: duckdb.DuckDBPyConnection, table: str, pk: str, updated_at: str, batch_date: str) -> Dict[str, int]:
    """
    Merge one day's batch from bronze_{table} into silver_{table}.
    Only keys that are new or have a newer updated_at are replaced. Everything else is left alone.
    """
    ensure_silver_table(con, table)

    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW inc_{table} AS
        SELECT * FROM bronze_{table}
        WHERE batch_date = '{batch_date}';
    """)

    incoming = con.execute(f"SELECT COUNT(*) FROM inc_{table};").fetchone()[0]

    # Newest version of each key in today's batch
    con.execute(f"DROP TABLE IF EXISTS tmp_changed_{table};")
    con.execute(f"""
        CREATE TEMP TABLE tmp_changed_{table} AS
        WITH latest AS (
            SELECT * EXCLUDE(rn)
            FROM (
                SELECT *,
                       ROW_NUMBER() OVER (
                           PARTITION BY {pk}
                           ORDER BY CAST({updated_at} AS TIMESTAMP) DESC,
                                    CAST(ingested_at AS TIMESTAMP) DESC
                       ) AS rn
                FROM inc_{table}
            )
            WHERE rn = 1
        )
        -- keep only keys that are new, or newer than what silver already has
        SELECT l.*
        FROM latest l
        LEFT JOIN silver_{table} s ON s.{pk} = l.{pk}
        WHERE s.{pk} IS NULL
           OR CAST(l.{updated_at} AS TIMESTAMP) > CAST(s.{updated_at} AS TIMESTAMP);
    """)

    inserts, updates = con.execute(f"""
        SELECT COUNT(*) FILTER (WHERE s.{pk} IS NULL),
               COUNT(*) FILTER (WHERE s.{pk} IS NOT NULL)
        FROM tmp_changed_{table} c
        LEFT JOIN silver_{table} s ON s.{pk} = c.{pk};
    """).fetchone()

    # Touch only the changed keys, in one transaction
    con.execute("BEGIN TRANSACTION;")
    try:
        con.execute(f"DELETE FROM silver_{table} WHERE {pk} IN (SELECT {pk} FROM tmp_changed_{table});")
        con.execute(f"INSERT INTO silver_{table} SELECT * FROM tmp_changed_{table};")
        con.execute("COMMIT;")
    except Exception:
        con.execute("ROLLBACK;")
        raise

    return {"incoming_rows": int(incoming), "inserts": int(inserts), "updates": int(updates)}


def build_silver_append_only(con: duckdb.DuckDBPyConnection, table: str, pk: str, batch_date: str) -> Dict[str, int]:
    ensure_silver_table(con, table)

    con.execute(f"""
        CREATE OR REPLACE TEMP VIEW inc_{table} AS
        SELECT * FROM bronze_{table}
        WHERE batch_date = '{batch_date}';
    """)

    incoming = con.execute(f"SELECT COUNT(*) FROM inc_{table};").fetchone()[0]

    inserts = con.execute(f"""
        SELECT COUNT(*)
        FROM inc_{table} i
        LEFT JOIN silver_{table} s
          ON i.{pk} = s.{pk}
        WHERE s.{pk} IS NULL;
    """).fetchone()[0]

    con.execute(f"""
        INSERT INTO silver_{table}
        SELECT i.*
        FROM inc_{table} i
        LEFT JOIN silver_{table} s
          ON i.{pk} = s.{pk}
        WHERE s.{pk} IS NULL;
    """)

    return {"incoming_rows": int(incoming), "inserts": int(inserts), "updates": 0}


def main() -> None:
    batch_date = os.getenv("BATCH_DATE", "").strip()
    if not batch_date:
        raise SystemExit("Set BATCH_DATE like: BATCH_DATE=2026-01-01 python -m src.transformations.build_silver")

    con = duckdb.connect("warehouse/ecomm.duckdb")
    con.execute("PRAGMA threads=4;")

    print(f"Building Silver for batch_date={batch_date}")

    # One row per table per run, so anyone can see what each run actually changed.
    # Append-only on purpose: a rerun shows up as its own row with nothing changed.
    con.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_merge_log (
            batch_date VARCHAR,
            table_name VARCHAR,
            incoming_rows BIGINT,
            inserts BIGINT,
            updates BIGINT,
            silver_rows_after BIGINT,
            run_started_at TIMESTAMP
        );
    """)

    run_started_at = datetime.now()

    def log(table: str, stats: Dict[str, int]) -> None:
        total = con.execute(f"SELECT COUNT(*) FROM silver_{table};").fetchone()[0]
        con.execute(
            "INSERT INTO pipeline_merge_log VALUES (?, ?, ?, ?, ?, ?, ?);",
            [batch_date, table, stats["incoming_rows"], stats["inserts"], stats["updates"], total, run_started_at],
        )

    for table, (pk, updated_at) in UPSERT_TABLES.items():
        stats = build_silver_upsert(con, table, pk, updated_at, batch_date)
        log(table, stats)
        print(f"silver_{table}: incoming={stats['incoming_rows']:,} inserts={stats['inserts']:,} updates={stats['updates']:,}")

    for table, pk in APPEND_ONLY_TABLES.items():
        stats = build_silver_append_only(con, table, pk, batch_date)
        log(table, stats)
        print(f"silver_{table}: incoming={stats['incoming_rows']:,} inserts={stats['inserts']:,}")

    con.close()


if __name__ == "__main__":
    main()
