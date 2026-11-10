CREATE TEMPORARY TABLE ready_parcels AS SELECT id, weight, zone FROM src_ex_dispatch.parcels WHERE NOT (weight IS NULL OR weight < 3);
CREATE TEMPORARY TABLE light_parcels AS SELECT id, weight, zone FROM src_ex_dispatch.parcels WHERE weight IS NULL OR weight < 3;
CREATE TABLE mart.dispatch_board_test AS
SELECT
    p.id,
    p.weight,
    p.zone,
    z.label
FROM ready_parcels p
LEFT JOIN src_ex_dispatch.zones z
    ON p.zone IS NOT DISTINCT FROM z.zone;
