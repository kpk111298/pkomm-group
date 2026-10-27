<picture>
  <source media="(prefers-color-scheme: dark)" srcset="../../brand/pkomm-cart/lockup-dark.svg">
  <img src="../../brand/pkomm-cart/lockup-light.svg" alt="Pkomm Cart" height="72">
</picture>

# Pkomm Cart

A fictional online store, part of [Pkomm Group](../../README.md). It sells across the US for now. The data is simulated; the pipeline and its tests are real.

The point of this business is one hard problem: **records keep changing after they first arrive.** An order moves from placed to shipped to delivered. A payment fails, then goes through two days later. A pipeline that only looks at today's new rows gets the past wrong, and one that reloads everything every night gets slower forever.

## What it answers

- How many orders came in each day, and what were they worth?
- How much of that money has actually been collected?
- How many payments are failing?
- How many shipped orders have been delivered?

## How the data flows

**Source → Bronze → Silver → Gold → Dashboard**

| Layer | What happens there |
|---|---|
| Source | The store's system drops one folder per day with what was new or changed that day: customers, products, orders, order items, payments, shipments. Late changes reach back up to 7 days, like a payment retried within the week. Each day has its own seed, so any day can be regenerated exactly. |
| Bronze | Every file loaded as it arrived, plus `batch_date`, `ingested_at` and `source_file`. Nothing is fixed here. Loading the same day again replaces that day's rows instead of adding a second copy. |
| Silver | One row per business key, the newest version by `updated_at`. Each run replaces only the keys that are new or newer, inside one transaction, and logs what it changed to `pipeline_merge_log`. Order items are append-only. |
| Gold | `gold_fact_orders` (one row per order with its latest payment and shipping status), `gold_kpi_daily`, and `gold_kpi_history`, a snapshot of the daily numbers as of every run. Money is stored as exact decimals. |

## Daily numbers (`gold_kpi_daily`)

| Column | Meaning |
|---|---|
| `orders_total` | Orders placed that day |
| `gross_revenue` | Value of every order placed that day, paid or not |
| `captured_revenue` | Value of the orders whose latest payment was collected. This is the number that moves when a late payment comes through |
| `aov` | Average order value |
| `payments_failed`, `payment_fail_rate` | Orders whose latest payment failed |
| `shipped_orders`, `delivered_orders`, `delivery_rate` | Shipped orders, and the share of them delivered |

## Run it

From this folder, with Python 3.12:

```bash
pip install -r requirements.txt

# One day (creates the warehouse on first run)
RUN_DATE=2026-01-26 python -m src.pipeline.run_daily

# A range of days
START_DATE=2026-01-01 END_DATE=2026-01-14 python -m src.pipeline.backfill

# Dashboard (builds January 5 to 18 on first start if there's no warehouse yet)
streamlit run dashboard/app.py
```

Every run records itself in `pipeline_runs`. The dashboard shows the daily numbers, how any past day's revenue settled run by run, and what each run inserted and updated.

## Tests

```bash
python -m pytest -q
```

The tests copy the project into a temporary folder, run five days from an empty warehouse and check:

- a fresh start builds every layer
- each day's data is different, and regenerating a day gives the same files byte for byte
- every day has revenue and every order has line items
- no source ID is ever reused for a different order
- silver has one row per key, and it is always the newest version
- a late payment changes past `captured_revenue`, and the new number matches a recount straight from the raw payments
- rerunning a day changes nothing in any layer, and the run log shows 0 rows inserted or updated
- the KPI history shows a past day settling, one snapshot per run

Write-up with real numbers: [Monday's revenue went up on Thursday. Nothing was broken.](https://pkomm.com/blog/late-payments-change-past-revenue/)

More detail: [incremental strategy](docs/incremental-strategy.md), [architecture](docs/architecture.md), [data dictionary](docs/data-dictionary.md).
