from __future__ import annotations

import duckdb
import pandas as pd
import streamlit as st


DB_PATH = "warehouse/ecomm.duckdb"
COMPANY_NAME = "Pkomm Cart"


@st.cache_data(ttl=30)
def load_kpi_daily() -> pd.DataFrame:
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute("SELECT * FROM gold_kpi_daily ORDER BY order_date").df()
    con.close()
    df["order_date"] = pd.to_datetime(df["order_date"]).dt.date
    return df


@st.cache_data(ttl=30)
def load_fact_orders() -> pd.DataFrame:
    con = duckdb.connect(DB_PATH, read_only=True)
    df = con.execute(
        """
        SELECT
          order_id,
          order_date,
          order_status,
          order_channel,
          customer_id,
          order_gross_amount,
          latest_payment_status,
          latest_shipping_status,
          shipping_state,
          order_created_ts,
          order_updated_ts
        FROM gold_fact_orders
        """
    ).df()
    con.close()
    df["order_date"] = pd.to_datetime(df["order_date"]).dt.date
    # Money actually collected: the order's latest payment was captured
    df["captured_amount"] = df["order_gross_amount"].where(df["latest_payment_status"] == "CAPTURED", 0.0)
    return df


@st.cache_data(ttl=30)
def load_ops_metadata() -> dict:
    con = duckdb.connect(DB_PATH, read_only=True)

    # Data through = max KPI date
    data_through = con.execute("SELECT MAX(order_date) FROM gold_kpi_daily").fetchone()[0]

    # Last run info (table may not exist yet)
    exists = con.execute("""
        SELECT COUNT(*)
        FROM information_schema.tables
        WHERE table_name = 'pipeline_runs'
    """).fetchone()[0]

    last = None
    if exists:
        last = con.execute("""
            SELECT run_date, run_ts, status
            FROM pipeline_runs
            ORDER BY run_ts DESC
            LIMIT 1
        """).fetchone()

    con.close()

    return {
        "data_through": data_through,
        "last_run": last,  # (run_date, run_ts, status) or None
    }


def fmt_money(x: float) -> str:
    if pd.isna(x):
        return "$0"
    return f"${x:,.2f}"


def fmt_pct(x: float) -> str:
    if pd.isna(x):
        return "0.00%"
    return f"{x*100:.2f}%"


st.set_page_config(page_title=f"{COMPANY_NAME} | Incremental Analytics", layout="wide")
st.title(f"{COMPANY_NAME} Analytics")
st.caption(f"{COMPANY_NAME} is a fictional company. All data is simulated.")

ops = load_ops_metadata()

# Small ops strip
ops_col1, ops_col2, ops_col3 = st.columns(3)
ops_col1.caption(f"Data through: {ops['data_through']}")
if ops["last_run"] is None:
    ops_col2.caption("Last pipeline run: not recorded yet")
    ops_col3.caption("Pipeline status: n/a")
else:
    run_date, run_ts, status = ops["last_run"]
    ops_col2.caption(f"Last pipeline run: {run_ts}")
    ops_col3.caption(f"Pipeline status: {status} (run_date={run_date})")

st.divider()

# --- Load data
kpi = load_kpi_daily()
facts = load_fact_orders()

if kpi.empty:
    st.error("gold_kpi_daily is empty. Run Gold build first: python -m src.models.build_gold")
    st.stop()

min_d = kpi["order_date"].min()
max_d = kpi["order_date"].max()

# --- Filters
st.sidebar.header("Filters")

date_range = st.sidebar.date_input(
    "Date range",
    value=(min_d, max_d),
    min_value=min_d,
    max_value=max_d,
)

if isinstance(date_range, tuple) and len(date_range) == 2:
    start_d, end_d = date_range
else:
    start_d, end_d = min_d, max_d

channels = sorted(facts["order_channel"].dropna().unique().tolist())
selected_channels = st.sidebar.multiselect("Order channel", options=channels, default=channels)

facts_f = facts[
    (facts["order_date"] >= start_d)
    & (facts["order_date"] <= end_d)
    & (facts["order_channel"].isin(selected_channels))
].copy()

# KPI view (date-based from gold_kpi_daily) — for channel filter, recompute from facts
if len(selected_channels) == len(channels):
    kpi_f = kpi[(kpi["order_date"] >= start_d) & (kpi["order_date"] <= end_d)].copy()
else:
    if facts_f.empty:
        kpi_f = pd.DataFrame(columns=kpi.columns)
    else:
        g = facts_f.groupby("order_date", as_index=False).agg(
            orders_total=("order_id", "count"),
            gross_revenue=("order_gross_amount", "sum"),
            captured_revenue=("captured_amount", "sum"),
            aov=("order_gross_amount", "mean"),
            orders_cancelled=("order_status", lambda s: (s == "CANCELLED").sum()),
            payments_failed=("latest_payment_status", lambda s: (s == "FAILED").sum()),
            payments_captured=("latest_payment_status", lambda s: (s == "CAPTURED").sum()),
            shipped_orders=("latest_shipping_status", lambda s: s.notna().sum()),
            delivered_orders=("latest_shipping_status", lambda s: (s == "DELIVERED").sum()),
        )
        g["delivery_rate"] = g.apply(lambda r: 0 if r["shipped_orders"] == 0 else r["delivered_orders"] / r["shipped_orders"], axis=1)
        g["payment_fail_rate"] = g.apply(lambda r: 0 if r["orders_total"] == 0 else r["payments_failed"] / r["orders_total"], axis=1)
        g["aov"] = g["aov"].round(2)
        g["delivery_rate"] = g["delivery_rate"].round(4)
        g["payment_fail_rate"] = g["payment_fail_rate"].round(4)
        kpi_f = g.sort_values("order_date")

# --- KPI Tiles
col1, col2, col2b, col3, col4, col5 = st.columns(6)

total_orders = int(facts_f["order_id"].nunique()) if not facts_f.empty else 0
total_revenue = float(facts_f["order_gross_amount"].sum()) if not facts_f.empty else 0.0
captured_revenue = float(facts_f["captured_amount"].sum()) if not facts_f.empty else 0.0
aov = float(facts_f["order_gross_amount"].mean()) if not facts_f.empty else 0.0
pay_fail_rate = float((facts_f["latest_payment_status"] == "FAILED").mean()) if not facts_f.empty else 0.0

shipped = facts_f["latest_shipping_status"].notna().sum() if not facts_f.empty else 0
delivered = (facts_f["latest_shipping_status"] == "DELIVERED").sum() if not facts_f.empty else 0
delivery_rate = 0.0 if shipped == 0 else delivered / shipped

col1.metric("Orders", f"{total_orders:,}")
col2.metric("Gross Revenue", fmt_money(total_revenue))
col2b.metric("Captured Revenue", fmt_money(captured_revenue), help="Orders whose latest payment was collected. Changes when a late payment comes through.")
col3.metric("AOV", fmt_money(aov))
col4.metric("Payment Fail Rate", fmt_pct(pay_fail_rate))
col5.metric("Delivery Rate", fmt_pct(delivery_rate))

st.divider()

# --- Trends
left, right = st.columns(2)

with left:
    st.subheader("Revenue by Day")
    if not kpi_f.empty:
        st.line_chart(kpi_f[["order_date", "gross_revenue", "captured_revenue"]].set_index("order_date"))
    else:
        st.info("No data for selected filters.")

with right:
    st.subheader("Orders by Day")
    if not kpi_f.empty:
        st.line_chart(kpi_f[["order_date", "orders_total"]].set_index("order_date"))
    else:
        st.info("No data for selected filters.")

mid1, mid2 = st.columns(2)

with mid1:
    st.subheader("Payment Fail Rate by Day")
    if not kpi_f.empty:
        st.line_chart(kpi_f[["order_date", "payment_fail_rate"]].set_index("order_date"))
    else:
        st.info("No data for selected filters.")

with mid2:
    st.subheader("Delivery Rate by Day")
    if not kpi_f.empty:
        st.line_chart(kpi_f[["order_date", "delivery_rate"]].set_index("order_date"))
    else:
        st.info("No data for selected filters.")

st.divider()

# --- Drill-down table
st.subheader("Order Drill-down")
st.caption("Filter by channel/date using the sidebar. Table shows up to 200 rows.")

sort_by = st.selectbox(
    "Sort by",
    ["order_updated_ts", "order_created_ts", "order_gross_amount"],
    index=0,
)
sort_desc = st.checkbox("Sort descending", value=True)

if not facts_f.empty:
    facts_show = facts_f.sort_values(sort_by, ascending=not sort_desc).head(200)
    st.dataframe(facts_show, use_container_width=True, hide_index=True)
else:
    st.info("No orders for selected filters.")
