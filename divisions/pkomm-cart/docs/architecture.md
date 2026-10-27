# Architecture

## Goal
An end-to-end store analytics system that loads only new or changed data each day, keeps the past correct when records change late, and powers a KPI dashboard.

## Layers
1. Source (daily drops): `data/source/day=YYYY-MM-DD/`
2. Ingestion: reads each drop and lands it in bronze
3. Bronze (raw): every file as it arrived; a rerun replaces only that day
4. Silver (clean + incremental merge): one row per key, newest `updated_at` wins
5. Gold (analytics): order fact table and daily KPIs
6. Serving: DuckDB + Streamlit dashboard
7. Orchestration: `run_daily.py` runs the steps in order and logs each run; `backfill.py` replays a range
8. Tests: `tests/test_pipeline.py` runs the whole thing end to end

## Key design choices
- Bronze is never edited, so any day can be traced and replayed
- Silver is the trusted layer, swapped in one transaction
- Gold is shaped for reporting, with money stored as exact decimals
