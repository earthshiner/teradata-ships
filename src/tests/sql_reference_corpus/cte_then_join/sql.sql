REPLACE VIEW {{DOM_V}}.Recent_Customers_V AS
WITH cte_recent AS (
    SELECT customer_id, MAX(order_dt) AS last_order
    FROM {{DOM_T}}.Orders
    GROUP BY customer_id
)
SELECT c.customer_id, c.name, r.last_order
FROM cte_recent r
INNER JOIN {{REF_T}}.Customer c ON c.customer_id = r.customer_id;
