CREATE TEMPORARY TABLE ready_parcels ENGINE=Memory AS SELECT id, weight, zone FROM src_ex_dispatch.parcels WHERE NOT (weight IS NULL OR weight < 3);
CREATE TEMPORARY TABLE light_parcels ENGINE=Memory AS SELECT id, weight, zone FROM src_ex_dispatch.parcels WHERE weight IS NULL OR weight < 3;
CREATE TABLE mart.dispatch_board_test
(
    `id` INTEGER NOT NULL,
    `weight` Nullable(INTEGER),
    `zone` Nullable(INTEGER),
    `label` Nullable(VARCHAR(16))
)
ENGINE = MergeTree
ORDER BY tuple()
AS
SELECT
    p.id,
    p.weight,
    p.zone,
    z.label
FROM ready_parcels AS p
LEFT JOIN src_ex_dispatch.zones AS z ON p.zone = z.zone
SETTINGS join_use_nulls = 1

;