from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import altair as alt
import duckdb
import pandas as pd
import streamlit as st


PROJECT = Path(__file__).resolve().parent.parent
DB_PATH = str(PROJECT / "warehouse" / "ecomm.duckdb")
COMPANY_NAME = "Pkomm Cart"

# Two weeks of data, built on first start when there is no warehouse yet (for example on a fresh deploy)
DEMO_START, DEMO_END = "2026-01-05", "2026-01-18"


@st.cache_data(ttl=30)
def load_kpi_daily() -> pd.DataFrame:
    con = connect_read_only()
    df = con.execute("SELECT * FROM gold_kpi_daily ORDER BY order_date").df()
    con.close()
    df["order_date"] = pd.to_datetime(df["order_date"]).dt.date
    return df


@st.cache_data(ttl=30)
def load_fact_orders() -> pd.DataFrame:
    con = connect_read_only()
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
    con = connect_read_only()

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


def connect_read_only() -> duckdb.DuckDBPyConnection:
    """Open the warehouse for reading. If a pipeline run holds the lock, wait a moment and try again."""
    for attempt in range(20):
        try:
            return duckdb.connect(DB_PATH, read_only=True)
        except duckdb.IOException:
            if attempt == 19:
                raise
            time.sleep(1)


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0] > 0


@st.cache_data(ttl=30)
def load_kpi_history() -> pd.DataFrame:
    con = connect_read_only()
    if not _table_exists(con, "gold_kpi_history"):
        con.close()
        return pd.DataFrame()
    df = con.execute("""
        SELECT as_of_date, order_date,
               CAST(gross_revenue AS DOUBLE) AS gross_revenue,
               CAST(captured_revenue AS DOUBLE) AS captured_revenue,
               payments_failed
        FROM gold_kpi_history
        ORDER BY order_date, as_of_date
    """).df()
    con.close()
    df["as_of_date"] = pd.to_datetime(df["as_of_date"]).dt.date
    df["order_date"] = pd.to_datetime(df["order_date"]).dt.date
    return df


@st.cache_data(ttl=30)
def load_merge_log() -> pd.DataFrame:
    con = connect_read_only()
    if not _table_exists(con, "pipeline_merge_log"):
        con.close()
        return pd.DataFrame()
    df = con.execute("""
        SELECT batch_date,
               run_started_at AS run_at,
               SUM(incoming_rows) AS rows_received,
               SUM(inserts) AS rows_inserted,
               SUM(updates) AS rows_updated,
               SUM(CASE WHEN table_name <> 'order_items' THEN silver_rows_after ELSE 0 END) AS silver_rows
        FROM pipeline_merge_log
        GROUP BY batch_date, run_started_at
        ORDER BY run_at DESC
    """).df()
    con.close()
    return df


def fmt_money(x: float) -> str:
    if pd.isna(x):
        return "$0"
    return f"${x:,.2f}"


def fmt_money_short(x: float) -> str:
    """For the tiles at the top, where a full number with cents gets cut off."""
    if pd.isna(x):
        return "$0"
    if abs(x) >= 1_000_000:
        return f"${x / 1_000_000:,.2f}M"
    return f"${x:,.0f}"


def fmt_pct(x: float) -> str:
    if pd.isna(x):
        return "0.00%"
    return f"{x*100:.2f}%"


st.set_page_config(page_title=f"{COMPANY_NAME} | Incremental Analytics", layout="wide")
st.title(f"{COMPANY_NAME} Analytics")
st.caption(f"{COMPANY_NAME} is a fictional company. All data is simulated.")

READY_MARKER = PROJECT / "warehouse" / ".demo_ready"
BUILDING_MARKER = PROJECT / "warehouse" / ".demo_building"


@st.cache_resource(show_spinner=False)
def ensure_demo_warehouse() -> str:
    """
    Build the demo data once per server, even when several visitors arrive at the same time.
    Streamlit runs this function once and makes every other session wait for it, so nobody
    tries to read the warehouse while the pipeline is still writing to it.
    Returns an empty string on success, or the error output if the build failed.
    """
    if READY_MARKER.exists():
        return ""

    # Start clean, in case an earlier start stopped halfway
    shutil.rmtree(PROJECT / "warehouse", ignore_errors=True)
    shutil.rmtree(PROJECT / "data" / "source", ignore_errors=True)
    (PROJECT / "warehouse").mkdir(parents=True, exist_ok=True)
    (PROJECT / "data" / "source").mkdir(parents=True, exist_ok=True)
    BUILDING_MARKER.touch()

    result = subprocess.run(
        [sys.executable, "-m", "src.pipeline.backfill"],
        cwd=PROJECT,
        env={**os.environ, "START_DATE": DEMO_START, "END_DATE": DEMO_END},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return (result.stdout[-3000:] + result.stderr[-3000:]) or "Pipeline exited with an error."

    READY_MARKER.touch()
    BUILDING_MARKER.unlink(missing_ok=True)
    return ""


def warehouse_is_usable() -> bool:
    """True when there is a finished warehouse we can read, for example one built locally."""
    if READY_MARKER.exists():
        return True
    if BUILDING_MARKER.exists() or not Path(DB_PATH).exists():
        # The pipeline is still writing between days, so wait for it
        return False
    try:
        con = connect_read_only()
        try:
            con.execute("SELECT COUNT(*) FROM gold_kpi_daily").fetchone()
        finally:
            con.close()
        return True
    except Exception:
        # Locked by a build that is still running, or left half built
        return False


if not warehouse_is_usable():
    with st.spinner(f"First start: running the daily pipeline for {DEMO_START} to {DEMO_END}. This takes about a minute."):
        error = ensure_demo_warehouse()
    if error:
        ensure_demo_warehouse.clear()
        st.error("The first build failed. Refresh the page to try again.")
        st.code(error)
        st.stop()
    st.cache_data.clear()

ops = load_ops_metadata()

# Small ops strip
ops_col1, ops_col2, ops_col3 = st.columns(3)
ops_col1.caption(f"Data through: {ops['data_through']}")
if ops["last_run"] is None:
    ops_col2.caption("Last pipeline run: not recorded yet")
    ops_col3.caption("Pipeline status: n/a")
else:
    run_date, run_ts, status = ops["last_run"]
    ops_col2.caption(f"Last pipeline run: {run_ts:%Y-%m-%d %H:%M}")
    ops_col3.caption(f"Last batch: {run_date} ({status.lower()})")

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
col2.metric("Gross Revenue", fmt_money_short(total_revenue))
col2b.metric("Captured", fmt_money_short(captured_revenue), help="Orders whose latest payment was collected. Changes when a late payment comes through.")
col3.metric("AOV", fmt_money_short(aov))
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

# --- How a past day settles
st.subheader("How a Day's Revenue Settles")
st.caption(
    "Gross revenue for a day is fixed once its orders are in. Captured revenue keeps rising "
    "for about a week as failed or pending payments go through. Each point is the number "
    "as it looked after that day's pipeline run."
)
hist = load_kpi_history()
if hist.empty:
    st.info("No history yet. It fills in as the daily pipeline runs.")
else:
    days_with_history = sorted(hist["order_date"].unique().tolist())
    pick = st.selectbox("Order date", days_with_history, index=0)
    h = hist[hist["order_date"] == pick].set_index("as_of_date")
    first, last = h.iloc[0], h.iloc[-1]
    s1, s2, s3 = st.columns(3)
    s1.metric("Gross revenue", fmt_money(last["gross_revenue"]))
    s2.metric(
        "Captured on the day",
        fmt_money(first["captured_revenue"]),
        help="What finance would have seen the morning after.",
    )
    s3.metric(
        "Captured now",
        fmt_money(last["captured_revenue"]),
        delta=fmt_money(last["captured_revenue"] - first["captured_revenue"]),
    )
    chart_df = (
        h[["gross_revenue", "captured_revenue"]]
        .rename(columns={"gross_revenue": "Gross", "captured_revenue": "Captured"})
        .reset_index()
        .melt(id_vars="as_of_date", var_name="Revenue", value_name="Amount")
    )
    chart_df["as_of_date"] = pd.to_datetime(chart_df["as_of_date"])
    st.altair_chart(
        alt.Chart(chart_df)
        .mark_line(point=True)
        .encode(
            x=alt.X("as_of_date:T", title="As of the run on", axis=alt.Axis(format="%b %d")),
            y=alt.Y("Amount:Q", title="Revenue ($)", scale=alt.Scale(zero=False), axis=alt.Axis(format="$~s")),
            color=alt.Color("Revenue:N", legend=alt.Legend(orient="bottom", title=None)),
            tooltip=[alt.Tooltip("as_of_date:T", title="As of", format="%b %d"), "Revenue:N", alt.Tooltip("Amount:Q", format="$,.2f")],
        )
        .properties(height=320, padding={"left": 70, "right": 12, "top": 8, "bottom": 8}),
        use_container_width=True,
    )

st.divider()

# --- What each run changed
st.subheader("What Each Run Changed")
st.caption(
    "Silver only replaces rows that are new or have a newer version. A rerun of the same day "
    "shows up here with nothing inserted or updated."
)
log = load_merge_log()
if log.empty:
    st.info("No runs logged yet.")
else:
    shown = log.head(30).rename(columns={
        "batch_date": "Batch",
        "run_at": "Run at",
        "rows_received": "Rows received",
        "rows_inserted": "New rows",
        "rows_updated": "Updated rows",
        "silver_rows": "Silver rows after",
    })
    st.dataframe(shown, use_container_width=True, hide_index=True)

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
