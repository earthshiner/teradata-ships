REPLACE VIEW {{DOM_V}}.Customer_Snapshot_V AS
SELECT c.customer_id, a.street, p.phone
FROM {{DOM_T}}.Customer c,
     {{REF_T}}.Address a,
     {{REF_T}}.Phone p
WHERE c.customer_id = a.customer_id
  AND c.customer_id = p.customer_id;
