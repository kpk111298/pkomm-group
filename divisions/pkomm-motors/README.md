<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../../brand/pkomm-motors/lockup-dark.svg">
  <img src="../../brand/pkomm-motors/lockup-light.svg" alt="Pkomm Motors" height="72">
</picture>

# Pkomm Motors

Five fictional car dealerships in Dallas, Chicago, Atlanta, Phoenix and Seattle, with a service bay at each. Part of [Pkomm Group](../../README.md). The data is simulated; the pipeline, tests and dashboard are real.

---

## What This Project Does

A dealer management system records each business day and exports it once. The pipeline adds that day to a raw layer, cleans and models it with dbt, and tests it before anything reaches the dashboard. Two rules hold it together:

- **Bad data never reaches the dashboard.** A failing test stops the business tables, and the dashboard keeps the last good numbers.
- **A closed day stays closed.** Once a day has closed, the loader refuses to load it again, and a test checks every build against the numbers recorded when it closed.

**Live dashboard:** [pkomm-group.streamlit.app](https://pkomm-group.streamlit.app). Pick Executive, Branch manager or Salesperson, then open How it runs. Run the next day, press Break it, or press Rewrite the past to watch a closed day refuse to change.

Or sign in by hand:
- Executive: `exec001` / `exec001`
- Branch Manager: `mgr001` through `mgr005` / same as username
- Salesperson: any salesperson ID from `EMP001` to `EMP035` / same as username

---

## Architecture
```
Dealer system (sources/dms)
One export per business day, seeded by the date, never rewritten
    ↓
Bronze layer (DuckDB)
Append-only, one batch per day: _batch_date, _ingested_at, _source_file
Closed days are refused (control.closed_days)
    ↓
Silver layer (dbt)
Cleaned, typed staging models. The newest version of each car wins
18 data tests, including the closed-days check
    ↓
Gold layer (dbt)
Daily sales, leaderboard, revenue vs target, inventory, service bay
6 more tests. A failing test upstream skips these (dbt build)
    ↓
Streamlit dashboard
Role based views, plus How it runs and Problems solved
```

---

## The dealer system

`sources/dms/dms.py` runs the dealership one day at a time from an opening lot on August 15, 2026. Each day is generated from its own seed (the date), so the same day always comes out the same. It writes a day's files once and refuses to write that day again.

It also behaves like a real dealership:

- Saturdays are the busiest day, and the last three days of a month see a push to hit targets
- No car sales on Sundays in Chicago (Illinois law) or Dallas, and the service bays close on Sundays
- Each branch has one salesperson who sells far more than the others
- A car is sold once, and cars that sit on the lot for more than 30 or 60 days are discounted
- New cars arrive on weekdays to keep each lot stocked

---

## Closing a day

A business day closes once the next day has loaded and the build passed. At that moment the loader records each branch's units and revenue for it in `control.closed_day_totals`. From then on:

1. The loader refuses any file for that day (`ClosedDayError`), so bronze can't change.
2. The `closed_days_unchanged` test recounts every closed day from the cleaned sales on each build. A difference fails the test, and dbt skips every business table that depends on sales.
3. The business tables count "this month" and "the last 7 days" from the latest day in the data, not from the server's clock, so the same closed days give the same answer tomorrow.

Problem: [001, yesterday's sales changed overnight](../../problems/001-history-rewrites-itself).

---

## Tech Stack

| Layer | Technology |
|---|---|
| Source system | Python, Faker |
| Data Warehouse | DuckDB |
| Transformations | dbt Core, dbt-duckdb |
| Tests | dbt tests, pytest |
| Dashboard | Streamlit, Plotly |
| Deployment | Streamlit Community Cloud |

---

## Data Model

**Reference tables:** locations, employees

**Daily tables:** sales_transactions, service_jobs, vehicles (one row per change to a car: arrived, sold)

**5 silver models:** fully typed and tested staging layers

**5 gold models:**
- `daily_sales_by_location`: revenue and unit trends per branch
- `salesperson_leaderboard`: ranked performance with commission tracking
- `revenue_vs_target`: monthly pacing against branch targets
- `inventory_status`: stock by make, status and location
- `service_center_utilization`: technician efficiency and revenue

---

## Data Quality

24 dbt tests covering:
- Primary key uniqueness
- Not null constraints on critical fields
- Accepted value validation
- Closed days matching the numbers recorded when they closed

The pipeline runs as one `dbt build`, so a table is tested before anything that depends on it gets built. If a test fails, the business tables are held back and the dashboard keeps the last good data. The How it runs tab shows this live: Break it plants 15 bad records and you can see which ones cleaning removes and which ones the tests stop.

Write-up: [Bad data will reach your raw layer. It just can't get past it.](https://pkomm.com/blog/stop-bad-data-before-the-dashboard/)

---

## Tests

```bash
python -m pytest -q
```

8 end-to-end tests build the history from nothing in a temporary folder and check that:

- the history builds and every dbt test passes
- the dealer system gives the same day every time, and never writes a day twice
- the next night closes yesterday and changes no closed day
- rerunning an open day replaces it instead of doubling it
- a closed day can't be loaded again
- a closed day changed behind the loader's back stops the business tables
- no business table reads the server's clock
- the dealership behaves like one: a car sells once, no Sunday sales in Illinois or Texas, Saturdays are busiest

---

## Running Locally
```bash
git clone https://github.com/kpk111298/pkomm-group
cd pkomm-group/divisions/pkomm-motors
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Build the history from nothing: export, load, dbt build, close the days
python -m orchestration.nightly history

# One more night
python -m orchestration.nightly day 2026-11-14

# Launch the dashboard
streamlit run dashboard/app.py
```
