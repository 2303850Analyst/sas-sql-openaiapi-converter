CREATE TABLE mart.sky_summary_test AS
SELECT
    sector,
    CAST(COUNT(*) AS INTEGER) AS n,
    CAST(COUNT(DISTINCT reading) AS INTEGER) AS uniq,
    CAST(SUM(reading) AS NUMERIC(28,12)) AS total_reading,
    AVG(reading)::DECIMAL(30,9) AS mean_reading,
    CAST(COUNT(reading) AS INTEGER) AS present
FROM src_ex_observatory.readings
GROUP BY sector
HAVING COUNT(*) > 0;
