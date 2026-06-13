REPLACE VIEW {{DOM_V}}.ProcessStatus_V AS
SELECT main.business_dt, main.status
FROM (
    SELECT business_dt, status
    FROM {{REF_T}}.ProcessRun
    WHERE status IN (
        SELECT Min(sRV_ProcessSumByBusDate.Process_State)
        FROM (
            SELECT business_dt, Process_State
            FROM {{REF_T}}.ProcessSum
        ) sRV_ProcessSumByBusDate
    )
) main;
