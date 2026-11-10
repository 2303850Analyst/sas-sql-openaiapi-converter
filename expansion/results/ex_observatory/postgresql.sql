CREATE TABLE mart.sky_summary_test AS
SELECT
    sector,
    CAST(COUNT(*) AS INTEGER) AS n,
    CAST(COUNT(DISTINCT reading) AS INTEGER) AS uniq,
    CAST(SUM(reading) AS NUMERIC(28,12)) AS total_reading,
    CASE
        WHEN COUNT(reading) = 0 THEN NULL
        ELSE CAST(
            ROUND(
                SUM(reading)::NUMERIC / COUNT(reading)::NUMERIC,
                9
            ) AS NUMERIC(30,9)
        )
    END AS mean_reading,
    CAST(COUNT(reading) AS INTEGER) AS present
FROM src_ex_observatory.readings
GROUP BY sector
HAVING COUNT(*) > 0
;