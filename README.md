<picture>
  <source media="(prefers-color-scheme: dark)" srcset="brand/pkomm-group/lockup-dark.svg">
  <img src="brand/pkomm-group/lockup-light.svg" alt="Pkomm Group" height="72">
</picture>

# Pkomm Group

**Pkomm Group is a fictional company.** I made it up to practise real data engineering problems, solve them, and write them up.

Practice projects usually start with clean data. Real jobs don't. So this company makes messy data on purpose: records that arrive late, history that changes, files that break. Each problem gets logged like a work ticket, fixed with free tools, tested, and written up on [pkomm.com/blog](https://pkomm.com/blog).

The businesses, customers and numbers are fictional and simulated. The code, tests and fixes are real.

## The businesses

| Business | What it does | Code |
|---|---|---|
| Pkomm Motors | Five car dealerships with service bays. Raw data in DuckDB, cleaned and modelled with dbt, 23 tests that gate the business tables, and a [live dashboard](https://pkomm-group.streamlit.app) with three role-based views and a Break it button | [divisions/pkomm-motors](divisions/pkomm-motors) |
| Pkomm Cart | An online store whose orders, payments and shipments change for days after they're placed. A daily incremental pipeline that merges only the rows that changed, 9 end-to-end tests, and a [live dashboard](https://pkomm-group-cart.streamlit.app) that shows any past day's revenue settling | [divisions/pkomm-cart](divisions/pkomm-cart) |

More businesses open one at a time, each when there's a real problem to solve in it.

## The problems

| # | Problem | Business | Status |
|---|---|---|---|
| 001 | [Yesterday's sales changed overnight](problems/001-history-rewrites-itself) | Pkomm Motors | Open |
| 002 | [The repo that grows every day](problems/002-database-in-git) | Pkomm Motors | Open |
| 003 | [Late payments flip the revenue number](problems/003-late-arriving-updates) | Pkomm Cart | Fixed, tested and written up |

Every write-up follows the same [template](problems/_template.md): the problem, the fix, why this way, what went wrong, proof, honest limits, and how it would run on AWS, Azure, Snowflake or Databricks.

## Writing

| Post | What it covers |
|---|---|
| [I built a company just to break its data](https://pkomm.com/blog/i-built-a-company-to-break-its-data/) | The tour: the businesses, how data comes in and how it gets used |
| [Bad data will reach your raw layer. It just can't get past it.](https://pkomm.com/blog/stop-bad-data-before-the-dashboard/) | Raw, cleaned and business layers with a test gate, proven by planting 15 bad records |

## Tools

Python and SQL, with DuckDB, dbt and Streamlit, all open source. The dashboard runs on Streamlit Community Cloud's free tier. Nothing costs money.

Each one stands in for a cloud service a real team would use. The [tool map](docs/tool-map.md) shows which, across AWS, Azure, Snowflake and Databricks, and every write-up explains what would carry over and what would change.

## History

Pkomm Motors started as `kommineni-automotive-pipeline` and Pkomm Cart as `ecommerce-incremental-analytics`. Both were merged here with their full commit history.

## License

All rights reserved. See [LICENSE](LICENSE).
