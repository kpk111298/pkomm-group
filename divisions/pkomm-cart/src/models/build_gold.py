from __future__ import annotations

import duckdb


def main() -> None:
    con = duckdb.connect("warehouse/ecomm.duckdb")
    con.execute("PRAGMA threads=4;")

    # ----------------------------
    # Dimensions
    # ----------------------------
    con.execute("DROP TABLE IF EXISTS gold_dim_customers;")
    con.execute("""
        CREATE TABLE gold_dim_customers AS
        SELECT
            customer_id,
            first_name,
            last_name,
            email,
            phone,
            city,
            state,
            country,
            customer_segment,
            created_at,
            updated_at
        FROM silver_customers;
    """)

    con.execute("DROP TABLE IF EXISTS gold_dim_products;")
    con.execute("""
        CREATE TABLE gold_dim_products AS
        SELECT
            product_id,
            product_name,
            category,
            brand,
            base_price,
            active_flag,
            created_at,
            updated_at
        FROM silver_products;
    """)

    # ----------------------------
    # Fact: order-level metrics
    # ----------------------------
    con.execute("DROP TABLE IF EXISTS gold_fact_orders;")
    con.execute("""
        CREATE TABLE gold_fact_orders AS
        WITH item_totals AS (
            SELECT
                order_id,
                -- money as exact decimals, so totals never drift between runs
                SUM(CAST(line_amount AS DECIMAL(14, 2))) AS order_gross_amount,
                SUM(quantity) AS total_quantity,
                COUNT(*) AS line_count
            FROM silver_order_items
            GROUP BY order_id
        ),
        pay AS (
            SELECT
                order_id,
                MAX_BY(payment_status, CAST(updated_at AS TIMESTAMP)) AS latest_payment_status,
                MAX_BY(payment_method, CAST(updated_at AS TIMESTAMP)) AS latest_payment_method,
                MAX(CAST(amount AS DECIMAL(14, 2))) AS payment_amount
            FROM silver_payments
            GROUP BY order_id
        ),
        ship AS (
            SELECT
                order_id,
                MAX_BY(shipping_status, CAST(updated_at AS TIMESTAMP)) AS latest_shipping_status,
                MAX_BY(carrier, CAST(updated_at AS TIMESTAMP)) AS latest_carrier,
                MAX(CAST(shipped_at AS TIMESTAMP)) AS shipped_at,
                MAX(CAST(delivered_at AS TIMESTAMP)) AS delivered_at
            FROM silver_shipments
            GROUP BY order_id
        )
        SELECT
            o.order_id,
            o.customer_id,
            CAST(o.created_at AS TIMESTAMP) AS order_created_ts,
            CAST(o.updated_at AS TIMESTAMP) AS order_updated_ts,
            CAST(o.created_at AS DATE) AS order_date,
            o.order_status,
            o.order_channel,
            o.shipping_city,
            o.shipping_state,
            o.shipping_country,

            COALESCE(it.order_gross_amount, CAST(0 AS DECIMAL(14, 2))) AS order_gross_amount,
            COALESCE(it.total_quantity, 0) AS total_quantity,
            COALESCE(it.line_count, 0) AS line_count,

            pay.latest_payment_status,
            pay.latest_payment_method,
            pay.payment_amount,

            ship.latest_shipping_status,
            ship.latest_carrier,
            ship.shipped_at,
            ship.delivered_at
        FROM silver_orders o
        LEFT JOIN item_totals it ON o.order_id = it.order_id
        LEFT JOIN pay ON o.order_id = pay.order_id
        LEFT JOIN ship ON o.order_id = ship.order_id;
    """)

    # ----------------------------
    # KPI table: daily rollups (dashboard source)
    # ----------------------------
    con.execute("DROP TABLE IF EXISTS gold_kpi_daily;")
    con.execute("""
        CREATE TABLE gold_kpi_daily AS
        WITH base AS (
            SELECT
                order_date,
                COUNT(*) AS orders_total,
                SUM(order_gross_amount) AS gross_revenue,
                SUM(CASE WHEN latest_payment_status = 'CAPTURED' THEN order_gross_amount ELSE CAST(0 AS DECIMAL(14, 2)) END) AS captured_revenue,
                AVG(order_gross_amount) AS aov,
                SUM(CASE WHEN order_status = 'CANCELLED' THEN 1 ELSE 0 END) AS orders_cancelled,
                SUM(CASE WHEN latest_payment_status = 'FAILED' THEN 1 ELSE 0 END) AS payments_failed,
                SUM(CASE WHEN latest_payment_status = 'CAPTURED' THEN 1 ELSE 0 END) AS payments_captured,
                SUM(CASE WHEN latest_shipping_status = 'DELIVERED' THEN 1 ELSE 0 END) AS delivered_orders,
                SUM(CASE WHEN latest_shipping_status IS NOT NULL THEN 1 ELSE 0 END) AS shipped_orders
            FROM gold_fact_orders
            GROUP BY order_date
        )
        SELECT
            order_date,
            orders_total,
            gross_revenue,
            captured_revenue,
            ROUND(aov, 2) AS aov,
            orders_cancelled,
            payments_failed,
            payments_captured,
            shipped_orders,
            delivered_orders,
            ROUND(CASE WHEN shipped_orders = 0 THEN 0 ELSE delivered_orders * 1.0 / shipped_orders END, 4) AS delivery_rate,
            ROUND(CASE WHEN orders_total = 0 THEN 0 ELSE payments_failed * 1.0 / orders_total END, 4) AS payment_fail_rate
        FROM base
        ORDER BY order_date;
    """)

    # Simple confirmation prints
    kpi_rows = con.execute("select count(*) from gold_kpi_daily").fetchone()[0]
    fact_rows = con.execute("select count(*) from gold_fact_orders").fetchone()[0]
    print(f"Built Gold tables. gold_fact_orders={fact_rows:,} rows | gold_kpi_daily={kpi_rows:,} rows")

    con.close()


if __name__ == "__main__":
    main()
