CREATE PROCEDURE {{PROC_DB}}.RefreshCustomerScores ()
BEGIN
    MERGE INTO {{TGT}}.Customer_Scores t
    USING (
        SELECT customer_id, AVG(score) AS avg_score
        FROM {{SRC}}.Score_Events
        WHERE event_dt >= CURRENT_DATE - 30
        GROUP BY customer_id
    ) s
    ON t.customer_id = s.customer_id
    WHEN MATCHED THEN UPDATE SET t.score = s.avg_score
    WHEN NOT MATCHED THEN INSERT (customer_id, score) VALUES (s.customer_id, s.avg_score);
END;
