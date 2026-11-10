\set ON_ERROR_STOP on
CREATE SCHEMA sky_raw;
CREATE SCHEMA sky_mart;
CREATE SCHEMA sky_expected;
CREATE TABLE sky_raw.stations (
  station_id integer PRIMARY KEY, station_name varchar(24), region varchar(8), active integer
);
CREATE TABLE sky_raw.calibration (
  station_id integer NOT NULL, sensor varchar(5) NOT NULL, calibration_seq integer NOT NULL,
  bias numeric(9,2), PRIMARY KEY (station_id, sensor, calibration_seq)
);
CREATE TABLE sky_raw.readings (
  event_id integer PRIMARY KEY, station_id integer, sensor varchar(5), observed_at timestamp,
  value numeric(9,2), missing_kind varchar(3), note varchar(40)
);
CREATE TABLE sky_raw.targets (
  station_id integer NOT NULL, month date NOT NULL, reading_target integer,
  PRIMARY KEY (station_id, month)
);
CREATE TABLE sky_mart.sas_monthly_kpi (
  station_id integer NOT NULL, month date NOT NULL, reading_count integer, total_value numeric(12,2),
  reading_target integer, missing_target integer, PRIMARY KEY (station_id, month)
);
CREATE TABLE sky_expected.monthly_kpi (LIKE sky_mart.sas_monthly_kpi INCLUDING ALL);
\copy sky_raw.stations FROM '/fixture/data/stations.csv' CSV HEADER
\copy sky_raw.calibration FROM '/fixture/data/calibration.csv' CSV HEADER
\copy sky_raw.readings FROM '/fixture/data/readings.csv' CSV HEADER
\copy sky_raw.targets FROM '/fixture/data/targets.csv' CSV HEADER
\copy sky_expected.monthly_kpi FROM '/fixture/expected/monthly_kpi.csv' CSV HEADER

\i /fixture/setup.sql
