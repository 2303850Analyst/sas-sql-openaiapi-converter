\set ON_ERROR_STOP on
-- Synthetic stand only. All generated candidates use the restricted role.
CREATE ROLE migration_validator LOGIN PASSWORD 'synthetic_local_validation'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
GRANT CONNECT,TEMPORARY ON DATABASE semantic_eval TO migration_validator;
