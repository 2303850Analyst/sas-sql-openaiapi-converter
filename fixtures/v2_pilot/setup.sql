-- Synthetic-only infrastructure; run with the local fixture owner, never by the SQL candidate.
CREATE TABLE IF NOT EXISTS sky_expected.active_ids (station_id integer);
TRUNCATE sky_expected.active_ids;
INSERT INTO sky_expected.active_ids VALUES (101),(102),(104),(105);

-- Independently specified checkpoints, based on the synthetic input CSV.
CREATE TABLE IF NOT EXISTS sky_expected.rejected (event_id integer, reason varchar(16));
TRUNCATE sky_expected.rejected;
INSERT INTO sky_expected.rejected
SELECT event_id, CASE WHEN station_id IS NULL THEN 'no station' ELSE 'missing measure' END
FROM sky_raw.readings
WHERE observed_at >= TIMESTAMP '2024-02-28' AND observed_at < TIMESTAMP '2024-03-03'
  AND (station_id IS NULL OR value IS NULL);

CREATE TABLE IF NOT EXISTS sky_expected.cleaned AS
SELECT event_id,station_id,sensor,observed_at,value AS measure,
       date_trunc('month',observed_at)::date AS month,
       (CASE WHEN value<=0 THEN 'non_positive' ELSE 'positive' END)::varchar(12) AS kind
FROM sky_raw.readings
WHERE observed_at >= TIMESTAMP '2024-02-28' AND observed_at < TIMESTAMP '2024-03-03'
  AND station_id IS NOT NULL AND value IS NOT NULL;

DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='migration_validator') THEN
    CREATE ROLE migration_validator LOGIN PASSWORD 'synthetic_local_validation';
  END IF;
END $$;
ALTER ROLE migration_validator NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT,TEMPORARY ON DATABASE observatory_lab TO migration_validator;
GRANT USAGE ON SCHEMA sky_raw,sky_expected TO migration_validator;
GRANT SELECT ON ALL TABLES IN SCHEMA sky_raw,sky_expected TO migration_validator;

-- Separate boundary case; the original 17 input observations remain unchanged.
CREATE SCHEMA IF NOT EXISTS sky_boundary;
CREATE SCHEMA IF NOT EXISTS sky_boundary_expected;
CREATE TABLE IF NOT EXISTS sky_boundary.readings AS TABLE sky_raw.readings;
CREATE TABLE IF NOT EXISTS sky_boundary.stations AS TABLE sky_raw.stations;
CREATE TABLE IF NOT EXISTS sky_boundary.calibration AS TABLE sky_raw.calibration;
CREATE TABLE IF NOT EXISTS sky_boundary.targets AS TABLE sky_raw.targets;
INSERT INTO sky_boundary.readings
SELECT 18,101,'THERM',TIMESTAMP '2024-02-28 00:00:00',1,NULL,'exact lower boundary'
WHERE NOT EXISTS (SELECT FROM sky_boundary.readings WHERE event_id=18);
CREATE TABLE IF NOT EXISTS sky_boundary_expected.cleaned AS TABLE sky_expected.cleaned;
INSERT INTO sky_boundary_expected.cleaned
SELECT 18,101,'THERM',TIMESTAMP '2024-02-28 00:00:00',1,DATE '2024-02-01','positive'
WHERE NOT EXISTS (SELECT FROM sky_boundary_expected.cleaned WHERE event_id=18);
CREATE TABLE IF NOT EXISTS sky_boundary_expected.rejected AS TABLE sky_expected.rejected;
CREATE TABLE IF NOT EXISTS sky_boundary_expected.monthly_kpi AS TABLE sky_expected.monthly_kpi;
UPDATE sky_boundary_expected.monthly_kpi SET reading_count=4,total_value=23.50
WHERE station_id=101 AND month=DATE '2024-02-01';
GRANT USAGE ON SCHEMA sky_boundary,sky_boundary_expected TO migration_validator;
GRANT SELECT ON ALL TABLES IN SCHEMA sky_boundary,sky_boundary_expected TO migration_validator;
