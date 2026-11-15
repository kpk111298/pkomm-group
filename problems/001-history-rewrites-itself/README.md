# 001: Yesterday's sales changed overnight

**Division:** Pkomm Motors
**Domain:** Incremental loading
**Level:** 2
**Status:** Fixed, tested and written up

## Where I've seen this on the job
At a national trucking company, I built raw, cleaned and reporting layers so analysts only query validated data, and closed days have to stay closed. This problem is what happens when the raw layer gets rebuilt instead of appended to.

## The ticket
The Chicago branch manager: Last Tuesday showed 3 cars sold on Wednesday morning. Today the same Tuesday shows 1. Which number do I report?

## What was actually wrong
Two things, one loud and one quiet.

1. **The source rebuilt history.** `data_generator/generate_data.py` used `random.seed(None)` and rebuilt the last 90 days of sales on every run, and bronze was reloaded from scratch. Ten runs of it gave Chicago's Tuesday, November 10 as 0, 1 or 3 cars, and the revenue for everything before November 1 swung from $16.57M to $18.38M. Between two runs, 79 of 91 days had a different number of cars sold. It also sold the same car up to 16 times in one run, because a sold car never left the lot.
2. **Two business tables read the server's clock.** `revenue_vs_target`, `salesperson_leaderboard` and `service_center_utilization` used `CURRENT_DATE`, so even with fixed data, "this month" and "the last 7 days" quietly moved every morning.

## Why it matters
Managers lose trust in the dashboard. Monthly targets and commissions depend on the past staying fixed.

## Constraints
Free tools only. The live dashboard must keep working.

## The fix
In `divisions/pkomm-motors`.
- `sources/dms/dms.py`: a dealer system that runs one business day at a time, seeds each day by its date, and never writes a day twice. Real patterns: busy Saturdays, month-end push, no Sunday sales in Illinois or Texas, a top seller per branch, lot-age discounts, a car sells once.
- `ingestion/ingest_bronze.py`: bronze is append-only by `_batch_date`. An open day can be reloaded (it replaces, never doubles). A closed day is refused.
- A day closes once the next day loads and the build passes. Its totals per branch go into `control.closed_day_totals`.
- `closed_days_unchanged`: a dbt test on the cleaned sales that recounts every closed day on each build. A difference fails it, and dbt skips every business table that depends on sales.
- The business tables count from the latest day in the data, not `CURRENT_DATE`.
- The old daily refresh workflow, which ran the rebuild and committed the result, is removed. It sat in the division folder, where GitHub never ran it after the repos merged.
- The dashboard's How it runs tab now runs the next day on top of the closed history, and Rewrite the past replays this ticket: the dealer system resends a closed Tuesday with one fewer Chicago sale, and the loader refuses it.

## Bugs found while building it
- The first version of the closed-days test referenced the control tables through `source()`. It failed correctly but stopped nothing, because dbt only treats a test as a model's gate when the test depends on that model alone. Naming the control tables directly fixed it.
- Once cars really left the lot when they sold, the branch manager's "Cars on the lot" table started listing sold cars too. It now shows only cars still on the lot, with how many days they've been there.

## Tests
`divisions/pkomm-motors/tests/test_closed_days.py` (8 tests) builds the history from nothing and checks: every dbt test passes, the same day comes out the same, the next night changes no closed day, rerunning an open day doesn't double it, a closed day can't be loaded again, a closed day changed behind the loader's back stops the business tables, no model reads the clock, and the dealership behaves like one.

## Write-up
[Yesterday's numbers should never change](https://pkomm.com/blog/a-closed-day-stays-closed/). Before and after numbers from real runs, the four rules, and the Rewrite the past button.

Live dashboard: [pkomm-group.streamlit.app](https://pkomm-group.streamlit.app)
