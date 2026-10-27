# 003: Late payments flip the revenue number

**Division:** Pkomm Cart
**Domain:** Incremental loading
**Level:** 2
**Status:** Fixed, tested and written up

## Where I've seen this on the job
At a Fortune 500 insurer, I loaded on-premises SQL Server data with watermark-based incremental loads, where records kept changing after the first load. At a national trucking company, log-based change data capture brought the same kind of late updates into Snowflake. Pkomm Cart is a small, public version of that problem.

## The ticket
Finance: Monday's collected revenue looked one way on Monday. By Thursday it was higher. Why does the past keep moving?

## What is actually wrong
Nothing, as long as it moves for the right reason. Payments that failed on Monday go through later in the week, so Monday's collected revenue really does go up. The real risks are a load that misses those late updates, or one that counts the same record twice.

## The fix
In `divisions/pkomm-cart`. Details in [incremental-strategy.md](../../divisions/pkomm-cart/docs/incremental-strategy.md).
- Bronze keeps every file as it arrived. Rerunning a day replaces that day instead of doubling it.
- Silver keeps one row per business key, the newest by `updated_at`. Each run replaces only the keys that changed, inside one transaction.
- `order_items` stays append-only.
- Gold splits revenue in two: `gross_revenue` (every order placed) and `captured_revenue` (orders whose latest payment was collected). Only the second one should move.

## Bugs found while testing
- A fresh start failed, because bronze tables were created without their load columns.
- Rerunning a day doubled bronze.
- Day 3 had no revenue and no line items, because the generator only looked at the day before and that day had no new products.
- New and extra shipments could get the same ID.
- Every day had the same number of orders, because the seed never changed.
- Revenue differed by tiny fractions between reruns, because money was stored as floats.

## Tests
`divisions/pkomm-cart/tests/test_pipeline.py` (9 tests) runs five days from an empty warehouse and checks a fresh start, replayable days, no reused IDs, newest-version silver, late payments matching a recount from raw, and that a rerun changes nothing.

## Write-up
[Monday's revenue went up on Thursday. Nothing was broken.](https://pkomm.com/blog/late-payments-change-past-revenue/) Two weeks of real runs, what the two easy pipelines get wrong, and how the merge keeps work flat as the data grows.
