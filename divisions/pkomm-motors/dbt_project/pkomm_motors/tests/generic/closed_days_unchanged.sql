-- A closed day stays closed.
--
-- When a business day closes, the loader records each branch's units and
-- revenue for it in control.closed_day_totals. On every build this test
-- recounts the closed days from the cleaned sales and compares. Any closed day
-- that now shows a different number, or a branch that appeared or disappeared,
-- is a failure. The test sits on the cleaned sales table, so dbt build skips
-- every business table that depends on it: a changed past never reaches the dashboard.
--
-- Used in staging/schema.yml on stg_sales_transactions.

-- The control tables are named directly instead of through source(). A test
-- that depends only on this model is the one dbt treats as this model's gate.

{% test closed_days_unchanged(model) %}

WITH closed AS (

    SELECT business_date FROM control.closed_days

),

recorded AS (

    SELECT business_date, location_id, units_sold, revenue
    FROM control.closed_day_totals

),

current_numbers AS (

    -- The same sums the gold table makes, straight from the cleaned sales
    SELECT
        sale_date_only AS business_date,
        location_id,
        COUNT(transaction_id) AS units_sold,
        CAST(ROUND(SUM(sale_price), 2) AS DECIMAL(14, 2)) AS revenue
    FROM {{ model }}
    WHERE sale_date_only IN (SELECT business_date FROM closed)
    GROUP BY 1, 2

)

SELECT
    COALESCE(r.business_date, c.business_date) AS business_date,
    COALESCE(r.location_id, c.location_id) AS location_id,
    r.units_sold AS units_when_closed,
    c.units_sold AS units_now,
    r.revenue AS revenue_when_closed,
    c.revenue AS revenue_now
FROM recorded r
FULL OUTER JOIN current_numbers c
    ON r.business_date = c.business_date
   AND r.location_id = c.location_id
WHERE r.units_sold IS DISTINCT FROM c.units_sold
   OR r.revenue IS DISTINCT FROM c.revenue

{% endtest %}
