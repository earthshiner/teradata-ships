CREATE PROCEDURE {{PROC_DB}}.RefreshCustomerSegments ()
BEGIN
    UPDATE {{TGT}}.Customer_Segment
    FROM (
        SELECT customer_id,
               CASE WHEN total_spend > 10000 THEN 'GOLD' ELSE 'SILVER' END AS segment
        FROM {{SRC}}.Customer_Spend
    ) src
    SET segment = src.segment
    WHERE {{TGT}}.Customer_Segment.customer_id = src.customer_id;
END;
