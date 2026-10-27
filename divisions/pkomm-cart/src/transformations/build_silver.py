from __future__ import annotations

import os
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
    Upsert into silver_{table} using incoming batch from bronze_{table}.
    IMPORTANT: materialize merge result before deleting silver to avoid losing history.
    """
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

    updates = con.execute(f"""
        SELECT COUNT(*)
        FROM inc_{table} i
        JOIN silver_{table} s
          ON i.{pk} = s.{pk}
        WHERE CAST(i.{updated_at} AS TIMESTAMP) > CAST(s.{updated_at} AS TIMESTAMP);
    """).fetchone()[0]

    # Materialize the merged latest-per-pk snapshot INTO A TEMP TABLE (not a view)
    con.execute(f"DROP TABLE IF EXISTS tmp_merged_{table};")
    con.execute(f"""
        CREATE TEMP TABLE tmp_merged_{table} AS
        SELECT * EXCLUDE(rn)
        FROM (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY {pk}
                    ORDER BY CAST({updated_at} AS TIMESTAMP) DESC,
                             CAST(ingested_at AS TIMESTAMP) DESC
                ) AS rn
            FROM (
                SELECT * FROM silver_{table}
                UNION ALL
                SELECT * FROM inc_{table}
            )
        )
        WHERE rn = 1;
    """)

    # Swap in the merged snapshot in one transaction, so a crash can't leave silver empty
    con.execute("BEGIN TRANSACTION;")
    try:
        con.execute(f"DELETE FROM silver_{table};")
        con.execute(f"INSERT INTO silver_{table} SELECT * FROM tmp_merged_{table};")
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

    for table, (pk, updated_at) in UPSERT_TABLES.items():
        stats = build_silver_upsert(con, table, pk, updated_at, batch_date)
        print(f"silver_{table}: incoming={stats['incoming_rows']:,} inserts={stats['inserts']:,} updates={stats['updates']:,}")

    for table, pk in APPEND_ONLY_TABLES.items():
        stats = build_silver_append_only(con, table, pk, batch_date)
        print(f"silver_{table}: incoming={stats['incoming_rows']:,} inserts={stats['inserts']:,}")

    con.close()


if __name__ == "__main__":
    main()
