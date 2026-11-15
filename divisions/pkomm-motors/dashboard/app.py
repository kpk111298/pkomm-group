# Pkomm Motors dashboard
#
# Three tabs:
#   Business view    what a manager sees, with a different view per role
#   How it runs      runs the real pipeline on demand and shows each step
#   Problems solved  the problems found in this business and their write-ups
#
# Pkomm Motors is a fictional company. All data is simulated.

import html
import os
import sys
import time
from datetime import date, datetime, timedelta

import duckdb
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import importlib  # noqa: E402
import pipeline  # noqa: E402

# Streamlit Cloud reruns app.py after a push but can keep an older pipeline
# module in memory. Reload it if it's missing what this file expects.
if not hasattr(pipeline, "GOLD"):
    pipeline = importlib.reload(pipeline)


def is_published(run):
    """Runs made before the quality gate existed have no flag; treat them as not published."""
    return bool(getattr(run, "published", False))
SAMPLE_DB = os.path.join(HERE, "..", "pkomm_motors.duckdb")
BRAND = os.path.join(HERE, "..", "..", "..", "brand", "pkomm-motors")
REPO = "https://github.com/kpk111298/pkomm-group/blob/main/divisions/pkomm-motors"
REPO_TREE = "https://github.com/kpk111298/pkomm-group/tree/main"

st.set_page_config(
    page_title="Pkomm Motors",
    page_icon=os.path.join(BRAND, "mark.png") if os.path.exists(os.path.join(BRAND, "mark.png")) else None,
    layout="wide",
    initial_sidebar_state="collapsed",
)

with open(os.path.join(HERE, "style.css")) as f:
    # Markdown ends an HTML block at the first blank line, so squash the
    # stylesheet onto one line before handing it over.
    css = " ".join(line.strip() for line in f if line.strip())
st.markdown(
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link href="https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@500;600'
    '&family=Jost:wght@400;500&display=swap" rel="stylesheet">'
    f"<style>{css}</style>",
    unsafe_allow_html=True,
)


def seal():
    path = os.path.join(BRAND, "mark.svg")
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


NAVY, GOLD, GOLD_INK, MOTORS = "#0A0D16", "#C9AE72", "#8A7344", "#7A2E2A"
GOOD, BAD, GRID, INK3 = "#2F6B45", "#A23B2E", "#EEEAE1", "#8A8F99"

# What each number means. Shown when you hover the ? next to a metric.
DEFINITIONS = {
    "revenue": "Sum of sale prices for cars sold in the period. Uses the cleaned sales table, so rows with a zero or negative price are already gone.",
    "units": "Number of sales in the period. One sale is one car.",
    "avg_deal": "Revenue divided by cars sold.",
    "financed": "Share of sales where the customer's loan was approved.",
    "on_track": "Branches whose month-to-date revenue is on pace for their monthly target. Uses the whole current month, so the filters above don't change it.",
    "target": "Monthly sales target for the branch, set in the branch table.",
    "commission": "Revenue times the salesperson's commission rate.",
    "best": "The single highest sale price in the period.",
}


# ------------------------------------------------------------
# Data access
# ------------------------------------------------------------

def active_db():
    # A run whose tests failed is held back, so the views keep the last good data.
    run = st.session_state.get("run")
    return run.db_path if run and is_published(run) else SAMPLE_DB


@st.cache_resource
def sample_connection():
    return duckdb.connect(SAMPLE_DB, read_only=True)


def q(sql):
    path = active_db()
    if path == SAMPLE_DB:
        return sample_connection().execute(sql).df()
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute(sql).df()
    finally:
        con.close()


def one(sql):
    return q(sql).iloc[0, 0]


@st.cache_data
def salespeople(path):
    con = duckdb.connect(path, read_only=True)
    try:
        return con.execute("""
            SELECT employee_id, full_name, location_id
            FROM main_silver.stg_employees
            WHERE is_salesperson = TRUE
            ORDER BY full_name
        """).df()
    finally:
        con.close()


def data_through():
    """Latest sale date in the data. Periods count back from here, not from
    today, so the views still work when the data is a few days old."""
    return pd.to_datetime(one("SELECT MAX(sale_date_only) FROM main_silver.stg_sales_transactions")).date()


def built_at():
    return pd.to_datetime(one("SELECT MAX(_ingested_at) FROM main_silver.stg_sales_transactions"))


def closed_days(path=None):
    """Closed business days and their total units, oldest first."""
    con = duckdb.connect(path or active_db(), read_only=True)
    try:
        return con.execute("""
            SELECT c.business_date, COALESCE(SUM(t.units_sold), 0) AS units
            FROM control.closed_days c
            LEFT JOIN control.closed_day_totals t ON t.business_date = c.business_date
            GROUP BY 1 ORDER BY 1""").df()
    finally:
        con.close()


def loaded_days(path=None):
    con = duckdb.connect(path or active_db(), read_only=True)
    try:
        return [r[0] for r in con.execute("SELECT DISTINCT batch_date FROM control.loads ORDER BY 1").fetchall()]
    finally:
        con.close()


# ------------------------------------------------------------
# Users. Demo logins, password is the same as the ID.
# ------------------------------------------------------------

BRANCHES = {"Dallas": "LOC001", "Chicago": "LOC002", "Atlanta": "LOC003",
            "Phoenix": "LOC004", "Seattle": "LOC005"}

USERS = {"exec001": {"role": "Executive", "name": "Group leadership", "location_id": None, "city": None}}
for i, (city, loc) in enumerate(BRANCHES.items(), start=1):
    USERS[f"mgr00{i}"] = {"role": "Branch Manager", "name": f"{city} branch", "location_id": loc, "city": city}

sp = salespeople(SAMPLE_DB)
for _, r in sp.iterrows():
    USERS[r["employee_id"]] = {"role": "Salesperson", "name": r["full_name"],
                               "location_id": r["location_id"], "employee_id": r["employee_id"], "city": None}


def sign_in(user_id):
    st.session_state.user = dict(USERS[user_id], user_id=user_id)


def sign_out():
    pipeline.cleanup(st.session_state.get("run"))
    for k in ("user", "run"):
        st.session_state.pop(k, None)


# ------------------------------------------------------------
# Small helpers
# ------------------------------------------------------------

def money(v):
    return f"${v:,.0f}"


def money_tile(v):
    """For the metric tiles, where a full seven-figure number gets cut off."""
    return f"${v / 1_000_000:,.2f}M" if abs(v) >= 1_000_000 else money(v)


def chart(fig, height):
    fig.update_layout(
        height=height, margin=dict(t=8, b=8, l=8, r=8),
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(family="Jost, sans-serif", size=12, color="#5A5F6B"),
        xaxis=dict(gridcolor=GRID, zeroline=False, showline=False),
        yaxis=dict(gridcolor=GRID, zeroline=False, showline=False),
        legend=dict(orientation="h", y=1.08, x=0, bgcolor="rgba(0,0,0,0)"),
        hoverlabel=dict(font_family="Jost, sans-serif"),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def head(title, text):
    st.markdown(f'<div class="pk-head"><h2>{title}</h2><p>{text}</p></div>', unsafe_allow_html=True)


def sub(text):
    st.markdown(f'<div class="pk-sub">{text}</div>', unsafe_allow_html=True)


def where(f, a="s"):
    parts = [f"{a}.sale_date_only BETWEEN '{f['start']}' AND '{f['end']}'"]
    if f.get("location_id"):
        parts.append(f"{a}.location_id = '{f['location_id']}'")
    if f.get("make"):
        parts.append(f"{a}.vehicle_id IN (SELECT vehicle_id FROM main_silver.stg_vehicles WHERE make = '{f['make']}')")
    if f.get("sale_type") == "Financed":
        parts.append(f"{a}.financing_approved = TRUE")
    elif f.get("sale_type") == "Cash":
        parts.append(f"{a}.financing_approved = FALSE")
    if f.get("employee_id"):
        parts.append(f"{a}.employee_id = '{f['employee_id']}'")
    return " AND ".join(parts)


# ------------------------------------------------------------
# Login
# ------------------------------------------------------------

def show_login():
    # Kept on joined lines on purpose: Markdown turns indented HTML into a code block.
    st.markdown(
        f'<div class="pk-login">{seal()}'
        '<h1>Pkomm Motors</h1>'
        '<p>Five dealerships in Dallas, Chicago, Atlanta, Phoenix and Seattle. Pkomm Motors is a '
        'fictional company built to practise real data engineering, and every number here is simulated.</p>'
        '<p class="pk-quiet" style="margin-top:22px">Pick a role to look around. Each one sees different data.</p>'
        '</div>',
        unsafe_allow_html=True,
    )
    _, a, b, c, _ = st.columns([1.2, 1, 1, 1, 1.2])
    if a.button("Executive", use_container_width=True, type="primary"):
        sign_in("exec001"); st.rerun()
    if b.button("Branch manager", use_container_width=True):
        sign_in("mgr002"); st.rerun()
    if c.button("Salesperson", use_container_width=True):
        sign_in(sp.iloc[0]["employee_id"]); st.rerun()

    _, mid, _ = st.columns([1.4, 2, 1.4])
    with mid.expander("Sign in with a demo ID instead"):
        uid = st.text_input("User ID", placeholder="exec001, mgr001 to mgr005, EMP001 to EMP020")
        pw = st.text_input("Password", type="password", placeholder="Same as the user ID")
        if st.button("Sign in", use_container_width=True):
            if uid in USERS and pw == uid:
                sign_in(uid); st.rerun()
            else:
                st.error("That ID and password don't match. The password is the same as the ID.")


# ------------------------------------------------------------
# Top bar
# ------------------------------------------------------------

def show_bar(user):
    run = st.session_state.get("run")
    if run and is_published(run):
        mins = int((time.time() - run.finished_at) // 60)
        ago = "just now" if mins < 1 else f"{mins} min ago"
        fresh = f'<span class="pk-fresh run"><i></i>Your run added {run.day:%b} {run.day.day}, {ago}</span>'
    elif run and getattr(run, "mode", "") != "rewrite":
        fresh = '<span class="pk-fresh held"><i></i>Your run was held back, showing the last good data</span>'
    else:
        last = data_through()
        fresh = f'<span class="pk-fresh"><i></i>Data through {last:%b} {last.day}</span>'
    role = {"Executive": "Executive, all branches",
            "Branch Manager": f"Branch manager, {user.get('city')}",
            "Salesperson": "Salesperson"}[user["role"]]
    left, right = st.columns([5, 1])
    left.markdown(
        f'<div class="pk-bar"><div class="pk-brand">{seal()}<div>'
        '<div class="name">Pkomm Motors</div>'
        '<div class="note">A fictional company. All data is simulated.</div></div></div>'
        f'<div class="pk-meta">{fresh}<span><b>{html.escape(user["name"])}</b> &middot; {role}</span></div></div>',
        unsafe_allow_html=True,
    )
    with right:
        st.write("")
        if st.button("Sign out", use_container_width=True):
            sign_out(); st.rerun()


# ------------------------------------------------------------
# Filters
# ------------------------------------------------------------

def show_filters(user):
    last = data_through()
    cols = st.columns([1.6, 1.1, 1.1, 1.2, 1.4])
    period = cols[0].radio("Period", ["Last 7 days", "Last 30 days", "Month to date"],
                           index=1, horizontal=True)
    start = {"Last 7 days": last - timedelta(days=6), "Last 30 days": last - timedelta(days=29),
             "Month to date": last.replace(day=1)}[period]

    if user["role"] == "Executive":
        branch = cols[1].selectbox("Branch", ["All branches"] + list(BRANCHES))
        location_id = BRANCHES.get(branch)
    else:
        location_id = user["location_id"]
        cols[1].selectbox("Branch", [f"{[c for c, l in BRANCHES.items() if l == location_id][0]}"], disabled=True)

    makes = q("SELECT DISTINCT make FROM main_silver.stg_vehicles ORDER BY make")["make"].tolist()
    make = cols[2].selectbox("Make", ["All makes"] + makes)
    sale_type = cols[3].radio("Sale type", ["All", "Financed", "Cash"], horizontal=True)

    employee_id = None
    if user["role"] != "Salesperson":
        people = salespeople(active_db())
        if location_id:
            people = people[people["location_id"] == location_id]
        who = cols[4].selectbox("Salesperson", ["Everyone"] + people["full_name"].tolist())
        if who != "Everyone":
            employee_id = people.loc[people["full_name"] == who, "employee_id"].iloc[0]
    else:
        employee_id = user["employee_id"]

    st.markdown(f'<p class="pk-quiet">Showing {start:%b %-d} to {last:%b %-d, %Y}. '
                f'Periods count back from the latest sale in the data.</p>', unsafe_allow_html=True)
    return {"start": start, "end": last, "location_id": location_id,
            "make": None if make == "All makes" else make, "sale_type": sale_type,
            "employee_id": employee_id}


# ------------------------------------------------------------
# Business view, one per role
# ------------------------------------------------------------

def show_executive(f):
    w = where(f)
    stats = q(f"""
        SELECT COUNT(*) AS units, COALESCE(SUM(sale_price), 0) AS revenue,
               COALESCE(AVG(CASE WHEN financing_approved THEN 1.0 ELSE 0 END), 0) AS fin
        FROM main_silver.stg_sales_transactions s WHERE {w}
    """).iloc[0]
    on_track = int(one("SELECT COUNT(*) FROM main_gold.revenue_vs_target WHERE status = 'On Track'"))
    units, revenue = int(stats.units), stats.revenue

    c = st.columns(5)
    c[0].metric("Revenue", money_tile(revenue), help=DEFINITIONS["revenue"])
    c[1].metric("Cars sold", f"{units:,}", help=DEFINITIONS["units"])
    c[2].metric("Average deal", money(revenue / units if units else 0), help=DEFINITIONS["avg_deal"])
    c[3].metric("Financed", f"{stats.fin * 100:.0f}%", help=DEFINITIONS["financed"])
    c[4].metric("Branches on track", f"{on_track} of 5", help=DEFINITIONS["on_track"])

    left, right = st.columns([3, 2])
    with left:
        sub("Revenue by branch")
        by_branch = q(f"""
            SELECT l.city, COALESCE(SUM(s.sale_price), 0) AS revenue
            FROM main_silver.stg_locations l
            LEFT JOIN main_silver.stg_sales_transactions s
              ON s.location_id = l.location_id AND {w}
            GROUP BY l.city ORDER BY revenue DESC
        """)
        fig = go.Figure(go.Bar(x=by_branch.city, y=by_branch.revenue, marker_color=NAVY,
                               hovertemplate="%{x}: $%{y:,.0f}<extra></extra>"))
        chart(fig, 280)
    with right:
        sub("This month against target")
        tgt = q("SELECT city, pct_of_target FROM main_gold.revenue_vs_target ORDER BY pct_of_target")
        fig = go.Figure(go.Bar(
            x=tgt.pct_of_target, y=tgt.city, orientation="h",
            marker_color=[GOOD if v >= 100 else GOLD for v in tgt.pct_of_target],
            text=[f"{v:.0f}%" for v in tgt.pct_of_target], textposition="outside",
            hovertemplate="%{y}: %{x:.1f}% of target<extra></extra>"))
        fig.add_vline(x=100, line_dash="dot", line_color=INK3)
        chart(fig, 280)

    sub("Daily revenue")
    trend = q(f"""SELECT sale_date_only AS day, SUM(sale_price) AS revenue
                  FROM main_silver.stg_sales_transactions s WHERE {w}
                  GROUP BY day ORDER BY day""")
    fig = go.Figure(go.Scatter(x=trend.day, y=trend.revenue, mode="lines", line=dict(color=MOTORS, width=2),
                               hovertemplate="%{x|%b %-d}: $%{y:,.0f}<extra></extra>"))
    chart(fig, 220)

    sub("Top salespeople")
    st.dataframe(q(f"""
        SELECT e.full_name AS "Salesperson", l.city AS "Branch",
               COUNT(*) AS "Cars sold", ROUND(SUM(s.sale_price)) AS "Revenue ($)",
               ROUND(SUM(s.sale_price) * e.commission_rate) AS "Commission ($)"
        FROM main_silver.stg_sales_transactions s
        JOIN main_silver.stg_employees e ON e.employee_id = s.employee_id
        JOIN main_silver.stg_locations l ON l.location_id = s.location_id
        WHERE {w}
        GROUP BY e.full_name, l.city, e.commission_rate
        ORDER BY "Revenue ($)" DESC LIMIT 10
    """), use_container_width=True, hide_index=True)


def show_manager(user, f):
    w = where(f)
    stats = q(f"""SELECT COUNT(*) AS units, COALESCE(SUM(sale_price), 0) AS revenue
                  FROM main_silver.stg_sales_transactions s WHERE {w}""").iloc[0]
    tgt = q(f"""SELECT monthly_target, pct_of_target, status FROM main_gold.revenue_vs_target
                WHERE location_id = '{user['location_id']}'""")

    c = st.columns(4)
    c[0].metric("Revenue", money_tile(stats.revenue), help=DEFINITIONS["revenue"])
    c[1].metric("Cars sold", f"{int(stats.units):,}", help=DEFINITIONS["units"])
    if len(tgt):
        t = tgt.iloc[0]
        c[2].metric("Monthly target", money_tile(t.monthly_target), f"{t.pct_of_target:.0f}% so far this month",
                    delta_color="off", help=DEFINITIONS["target"])
        c[3].metric("This month", "On track" if t.status == "On Track" else "Behind pace", help=DEFINITIONS["on_track"])

    left, right = st.columns([3, 2])
    with left:
        sub("Daily revenue")
        trend = q(f"""SELECT sale_date_only AS day, SUM(sale_price) AS revenue
                      FROM main_silver.stg_sales_transactions s WHERE {w}
                      GROUP BY day ORDER BY day""")
        chart(go.Figure(go.Bar(x=trend.day, y=trend.revenue, marker_color=NAVY,
                               hovertemplate="%{x|%b %-d}: $%{y:,.0f}<extra></extra>")), 260)
    with right:
        sub("Team")
        st.dataframe(q(f"""
            SELECT e.full_name AS "Salesperson", COUNT(s.transaction_id) AS "Cars sold",
                   ROUND(COALESCE(SUM(s.sale_price), 0)) AS "Revenue ($)"
            FROM main_silver.stg_employees e
            LEFT JOIN main_silver.stg_sales_transactions s ON s.employee_id = e.employee_id AND {w}
            WHERE e.location_id = '{user['location_id']}' AND e.is_salesperson
            GROUP BY e.full_name ORDER BY "Revenue ($)" DESC
        """), use_container_width=True, hide_index=True)

    left, right = st.columns(2)
    with left:
        sub("Cars on the lot")
        make_filter = f"AND make = '{f['make']}'" if f.get("make") else ""
        st.dataframe(q(f"""
            SELECT make AS "Make", COUNT(*) AS "Cars", ROUND(AVG(list_price)) AS "Avg list price ($)",
                   ROUND(AVG(DATE '{data_through()}' - received_date)) AS "Avg days on lot"
            FROM main_silver.stg_vehicles
            WHERE location_id = '{user['location_id']}' AND status <> 'sold' {make_filter}
            GROUP BY make ORDER BY make
        """), use_container_width=True, hide_index=True)
    with right:
        sub("Service bay")
        st.dataframe(q(f"""
            SELECT e.full_name AS "Technician", COUNT(j.job_id) AS "Jobs",
                   ROUND(COALESCE(SUM(j.labor_revenue), 0)) AS "Labor revenue ($)",
                   ROUND(AVG(j.efficiency_ratio), 2) AS "Efficiency"
            FROM main_silver.stg_employees e
            LEFT JOIN main_silver.stg_service_jobs j
              ON j.technician_id = e.employee_id AND j.job_date_only BETWEEN '{f['start']}' AND '{f['end']}'
            WHERE e.location_id = '{user['location_id']}' AND NOT e.is_salesperson
            GROUP BY e.full_name ORDER BY "Labor revenue ($)" DESC
        """), use_container_width=True, hide_index=True)


def show_salesperson(user, f):
    w = where(f)
    s = q(f"""SELECT COUNT(*) AS deals, COALESCE(SUM(sale_price), 0) AS revenue,
                     COALESCE(MAX(sale_price), 0) AS best
              FROM main_silver.stg_sales_transactions s WHERE {w}""").iloc[0]
    rate = one(f"SELECT commission_rate FROM main_silver.stg_employees WHERE employee_id = '{user['employee_id']}'")

    c = st.columns(4)
    c[0].metric("Cars sold", int(s.deals), help=DEFINITIONS["units"])
    c[1].metric("Revenue", money_tile(s.revenue), help=DEFINITIONS["revenue"])
    c[2].metric("Commission", money(s.revenue * rate), f"{rate * 100:.1f}% rate", delta_color="off",
                help=DEFINITIONS["commission"])
    c[3].metric("Best sale", money(s.best), help=DEFINITIONS["best"])

    others = dict(f, employee_id=None, location_id=None)
    left, right = st.columns([2, 3])
    with left:
        sub("Where you rank")
        st.dataframe(q(f"""
            SELECT RANK() OVER (ORDER BY SUM(s.sale_price) DESC) AS "Rank", e.full_name AS "Salesperson",
                   ROUND(SUM(s.sale_price)) AS "Revenue ($)"
            FROM main_silver.stg_sales_transactions s
            JOIN main_silver.stg_employees e ON e.employee_id = s.employee_id
            WHERE {where(others)}
            GROUP BY e.full_name ORDER BY "Rank" LIMIT 10
        """), use_container_width=True, hide_index=True)
    with right:
        sub("Your sales")
        mine = q(f"""SELECT transaction_id AS "Sale", sale_date_only AS "Date",
                            ROUND(sale_price) AS "Price ($)", financing_approved AS "Financed"
                     FROM main_silver.stg_sales_transactions s WHERE {w} ORDER BY "Date" DESC""")
        if len(mine):
            st.dataframe(mine, use_container_width=True, hide_index=True)
        else:
            st.info("No sales in this period. Try a longer period or clear the filters.")


def business_view(user):
    run = st.session_state.get("run")
    if run and is_published(run):
        st.markdown(f'<div class="pk-callout">You\'re looking at your own run: {run.day:%A, %B} {run.day.day} '
                    f'added on top of the closed history. Every earlier day is exactly as it was. Your run '
                    f'disappears when you sign out.</div>', unsafe_allow_html=True)
    elif run and getattr(run, "mode", "") == "rewrite":
        pass
    elif run:
        st.markdown('<div class="pk-callout bad">Your last run failed its tests, so its business tables '
                    'were held back. This view still shows the last good data, the way a real dashboard '
                    'should.</div>', unsafe_allow_html=True)
    f = show_filters(user)
    if user["role"] == "Executive":
        show_executive(f)
    elif user["role"] == "Branch Manager":
        show_manager(user, f)
    else:
        show_salesperson(user, f)


# ------------------------------------------------------------
# How it runs
# ------------------------------------------------------------

STEP_CODE = {
    "Export the day": f"{REPO}/sources/dms/dms.py",
    "Plant problems": f"{REPO}/dashboard/pipeline.py",
    "Load raw layer": f"{REPO}/ingestion/ingest_bronze.py",
    "Clean and model with dbt": f"{REPO}/dbt_project/pkomm_motors/models/staging",
    "Run data tests": f"{REPO}/dbt_project/pkomm_motors/models/staging/schema.yml",
    "Build business tables": f"{REPO}/dbt_project/pkomm_motors/models/marts",
    "Dealer system resends a closed day": f"{REPO}/dashboard/pipeline.py",
    "Check the closed day": f"{REPO}/dbt_project/pkomm_motors/tests/generic/closed_days_unchanged.sql",
}
PLAN = ["Export the day", "Load raw layer", "Clean and model with dbt",
        "Run data tests", "Build business tables"]
PLAN_REWRITE = ["Dealer system resends a closed day", "Load raw layer", "Check the closed day"]


def steps_html(done, planned):
    rows = []
    names = [s.name for s in done]
    todo = [p for p in planned if p not in names]
    for i, s in enumerate(done, start=1):
        cls, state = ("done", "Done") if s.ok else ("fail", "Failed")
        state = getattr(s, "state", "") or state
        link = STEP_CODE.get(s.name)
        code = f' &middot; <a href="{link}" target="_blank">code</a>' if link else ""
        rows.append(f'<div class="pk-step {cls}"><div class="n">{i}</div>'
                    f'<div class="what"><b>{s.name}</b><span>{html.escape(s.detail)}{code}</span></div>'
                    f'<div class="t">{s.seconds:.1f}s</div><div class="s">{state}</div></div>')
    for j, name in enumerate(todo, start=len(done) + 1):
        rows.append(f'<div class="pk-step wait"><div class="n">{j}</div>'
                    f'<div class="what"><b>{name}</b><span>Waiting</span></div>'
                    f'<div class="t"></div><div class="s"></div></div>')
    return f'<div class="pk-steps">{"".join(rows)}</div>'


def ledger_html(closed, loaded, mark=None):
    """The books: one square per business day, a column per week, Monday at the top.
    Closed days are filled, the open day is outlined, a day someone tried to change is ringed."""
    if closed.empty:
        return ""
    units = {d.date() if hasattr(d, "date") else d: int(u) for d, u in zip(closed.business_date, closed.units)}
    first, last = loaded[0], loaded[-1]
    week = first - timedelta(days=first.weekday())
    columns = []
    while week <= last:
        # Label the first column, and every week that holds the 1st of a month
        firsts = [week + timedelta(days=k) for k in range(7) if (week + timedelta(days=k)).day == 1]
        month = (firsts[0] if firsts else week).strftime("%b") if (firsts or not columns) else ""
        cells = []
        for k in range(7):
            day = week + timedelta(days=k)
            label = f"{day:%a, %b} {day.day}"
            if day in units:
                n = units[day]
                cls = "closed hit" if day == mark else "closed"
                tip = f"{label}: closed, {n} {'car' if n == 1 else 'cars'} sold"
            elif first <= day <= last:
                cls, tip = "open", f"{label}: open until the next day loads"
            else:
                cls, tip = "none", ""
            cells.append(f'<i class="{cls}" title="{tip}"></i>')
        columns.append(f'<div class="wk"><b>{month}</b>{"".join(cells)}</div>')
        week += timedelta(days=7)
    days_col = '<div class="wk dow"><b></b><span>Mon</span><span></span><span>Wed</span><span></span><span>Fri</span><span></span><span>Sun</span></div>'
    first_closed, last_closed = min(units), max(units)
    still_open = [d for d in loaded if d not in units]
    open_txt = " and ".join(f"{d:%b} {d.day}" for d in still_open) or "none"
    note = (f'{len(units)} closed days, {first_closed:%b} {first_closed.day} to {last_closed:%b} {last_closed.day}. '
            f'Open: {open_txt}.')
    return (f'<div class="pk-ledger"><div class="grid">{days_col}{"".join(columns)}</div>'
            f'<div class="key"><span><i class="closed"></i>Closed</span><span><i class="open"></i>Open</span>'
            + ('<span><i class="closed hit"></i>Resend refused</span>' if mark else "")
            + f'<span class="pk-quiet">{note}</span></div></div>')


def health_html(run):
    planted_col = run.broke_it
    rows = []
    for h in run.health:
        tone = {"None found": "pk-quiet", "Removed by the cleaning step": "pk-warn",
                "Got through cleaning, stopped by a test": "pk-ok",
                "Got through unnoticed": "pk-bad"}[h["outcome"]]
        planted = f'<td class="num">{run.planted.get(h["key"], 0)}</td>' if planted_col else ""
        rows.append(f'<tr><td>{h["label"]}</td>{planted}<td class="num">{h["found"]}</td>'
                    f'<td class="num">{h["left"]}</td><td class="{tone}">{h["outcome"]}</td></tr>')
    th = "<th>Planted</th>" if planted_col else ""
    return (f'<table class="pk-table"><tr><th>Problem</th>{th}<th>Found in raw</th>'
            f'<th>Left after cleaning</th><th>What happened</th></tr>{"".join(rows)}</table>')


def how_it_runs():
    head("How it runs",
         "The dealership's books run from August 15 to November 13, 2026, and every day before the last one "
         "is closed. Press run and this server does what the nightly job does: the dealer system exports "
         "the next day, it lands in the raw layer, dbt cleans, models and tests it, and if every test passes "
         "the day before closes for good. Break it slips bad records into that new day. Rewrite the past "
         "replays the ticket behind problem 001: the dealer system tries to resend a closed day with different "
         "numbers.")

    a, b, c, d = st.columns([1.15, 1.1, 1.1, 1.65])
    go_run = a.button("Run the next day", type="primary", use_container_width=True)
    go_break = b.button("Break it, then run", use_container_width=True)
    go_rewrite = c.button("Rewrite the past", use_container_width=True)
    with d.expander("Replay a Break it run"):
        replay = st.number_input("Seed", min_value=1, max_value=99999, value=None, step=1,
                                 help="Picks which records Break it damages. The same seed always breaks "
                                      "the same records. Leave empty for a new one.")

    box = st.empty()

    if go_run or go_break or go_rewrite:
        if go_rewrite:
            planned = PLAN_REWRITE
        else:
            planned = PLAN[:1] + (["Plant problems"] if go_break else []) + PLAN[1:]
        live = []
        box.markdown(steps_html(live, planned), unsafe_allow_html=True)

        def on_step(step):
            live.append(step)
            box.markdown(steps_html(live, planned), unsafe_allow_html=True)

        previous = st.session_state.get("run")
        try:
            result = pipeline.run(seed=int(replay) if replay else None, break_it=go_break,
                                  rewrite=go_rewrite, on_step=on_step)
        except pipeline.PipelineBusy:
            st.info("Someone else is running the pipeline right now. Give it a few seconds and try again.")
            return
        pipeline.cleanup(previous)
        st.session_state.run = result
        st.rerun()  # so the Business view picks up the new data straight away

    run = st.session_state.get("run")
    if not run:
        box.markdown(steps_html([], PLAN), unsafe_allow_html=True)
        st.markdown('<p class="pk-quiet">Nothing has run yet in your session. Each run takes about '
                    '10 seconds, uses its own copy of the warehouse, and is deleted when you sign out.</p>',
                    unsafe_allow_html=True)
        sub("The books")
        st.markdown(ledger_html(closed_days(pipeline.SAMPLE_DB), loaded_days(pipeline.SAMPLE_DB)),
                    unsafe_allow_html=True)
        return

    box.markdown(steps_html(run.steps, [s.name for s in run.steps]), unsafe_allow_html=True)

    if getattr(run, "mode", "") == "rewrite":
        rw = run.rewrite
        unit = lambda n: f"{n} {'car' if n == 1 else 'cars'}"
        tone = "" if rw["refused"] and rw["after"] == rw["before"] else " bad"
        st.markdown(f'<div class="pk-callout{tone}">The dealer system resent {rw["day"]:%A, %B} {rw["day"].day} '
                    f'saying {rw["city"]} sold {unit(rw["resent"])}. That day is closed, so the loader refused '
                    f'the file and nothing changed. This is the ticket behind problem 001, and the old pipeline '
                    f'would have quietly replaced the day.</div>', unsafe_allow_html=True)
        st.markdown(
            '<table class="pk-table pk-compare"><tr><th></th><th>Before</th><th>What the resend said</th>'
            '<th>After</th></tr>'
            f'<tr><td>{rw["city"]}, {rw["day"]:%B} {rw["day"].day}</td><td class="num">{unit(rw["before"])}</td>'
            f'<td class="num pk-bad">{unit(rw["resent"])}</td><td class="num pk-ok">{unit(rw["after"])}</td></tr>'
            '</table>', unsafe_allow_html=True)
        sub("The books")
        st.markdown(ledger_html(closed_days(run.db_path), loaded_days(run.db_path), mark=rw["day"]),
                    unsafe_allow_html=True)
        return

    failed = run.tests_failed
    total = sum(s.seconds for s in run.steps)
    if failed:
        held = next(s.detail for s in run.steps if s.name == "Build business tables")
        st.markdown(f'<div class="pk-callout bad">{len(failed)} of {len(run.tests)} tests failed, so the '
                    f'business tables were held back ({held.split(": ", 1)[-1]}). The dashboard kept the last '
                    f'good numbers, and no day was closed. That is the quality gate doing its job. Ran in '
                    f'{total:.1f}s, Break it seed {run.seed}.</div>', unsafe_allow_html=True)
    else:
        newly = run.closed.get("newly") or []
        closed_txt = " and ".join(f"{d:%B} {d.day}" for d in newly)
        st.markdown(f'<div class="pk-callout">All {len(run.tests)} tests passed in {total:.1f}s. '
                    f'{run.day:%B} {run.day.day} is in, {closed_txt or "no day"} closed, and the '
                    f'{run.closed.get("before_days", 0)} days closed before this run are exactly as they were. '
                    f'The Business view now shows this run\'s data.</div>', unsafe_allow_html=True)

    sub("The books")
    st.markdown(ledger_html(closed_days(run.db_path), loaded_days(run.db_path)), unsafe_allow_html=True)

    sub("Data health")
    st.markdown(health_html(run), unsafe_allow_html=True)
    if any(h["outcome"] == "Removed by the cleaning step" and h["found"] for h in run.health):
        st.markdown('<p class="pk-quiet" style="margin-top:10px">Honest note: the cleaning step drops bad '
                    'rows without keeping them anywhere. A real team would set them aside in a quarantine '
                    'table with the reason. That fix is on the list.</p>', unsafe_allow_html=True)

    sub("Tests")
    order = {"fail": 0, "error": 0, "skipped": 1, "pass": 2}
    label = {"fail": ("Failed", "pk-bad"), "error": ("Error", "pk-bad"),
             "skipped": ("Skipped, table held back", "pk-warn"), "pass": ("Passed", "pk-ok")}
    tests = sorted(run.tests, key=lambda t: (order.get(t["status"], 0), t["name"]))
    rows = "".join(
        f'<tr><td>{t["name"]}</td><td class="{label.get(t["status"], ("?", ""))[1]}">'
        f'{label.get(t["status"], (t["status"], ""))[0]}</td>'
        f'<td class="num">{t["failures"] or ""}</td></tr>' for t in tests)
    with st.expander(f"All {len(tests)} tests", expanded=bool(failed)):
        st.markdown(f'<table class="pk-table"><tr><th>Test</th><th>Result</th><th>Bad rows</th></tr>{rows}</table>',
                    unsafe_allow_html=True)


# ------------------------------------------------------------
# Problems solved
# ------------------------------------------------------------

PROBLEMS = [
    ("001", "Yesterday's sales changed overnight",
     "The old generator rebuilt the last 90 days on every run, so closed days kept changing. "
     "Now the dealer system exports one day at a time and closed days are locked.",
     "Fixed", "problems/001-history-rewrites-itself"),
    ("002", "The repo that grows every day",
     "A daily job saved the whole database into git, 208 commits and counting.",
     "Open", "problems/002-database-in-git"),
]


def problems():
    head("Problems solved", "Each problem in this business is logged like a work ticket, fixed, tested and "
         "written up. The tickets, code and tests live on GitHub.")
    rows = "".join(
        f'<div class="pk-problem"><div class="n">{n}</div><div><h4>{t}</h4><p>{d} '
        f'<a href="{REPO_TREE}/{path}" target="_blank">Read the ticket</a></p></div>'
        f'<div class="st">{s}</div></div>' for n, t, d, s, path in PROBLEMS)
    st.markdown(rows, unsafe_allow_html=True)
    st.markdown(f'<p class="pk-quiet" style="margin-top:16px">Write-ups go up on '
                f'<a href="https://pkomm.com/blog" target="_blank">pkomm.com/blog</a> as each one is finished.</p>',
                unsafe_allow_html=True)


# ------------------------------------------------------------
# Page
# ------------------------------------------------------------

user = st.session_state.get("user")
if not user:
    show_login()
else:
    show_bar(user)
    tab_business, tab_runs, tab_problems = st.tabs(["Business view", "How it runs", "Problems solved"])
    with tab_business:
        business_view(user)
    with tab_runs:
        how_it_runs()
    with tab_problems:
        problems()
