CREATE PROCEDURE {{PROC_DB}}.LoadDailyFacts ()
BEGIN
    INSERT INTO {{TGT}}.Daily_Facts (business_dt, customer_id, total)
    SELECT s.business_dt, s.customer_id, SUM(s.amount)
    FROM {{SRC}}.Sale_Events s
    INNER JOIN {{REF_T}}.Customer c ON s.customer_id = c.customer_id
    WHERE s.business_dt = CURRENT_DATE - 1
    GROUP BY s.business_dt, s.customer_id;
END;
