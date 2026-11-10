CREATE TABLE mart.catalogue_test
(
    id INTEGER,
    pages Nullable(INTEGER)
)
ENGINE = MergeTree()
ORDER BY tuple()
AS
SELECT
    id,
    pages
FROM src_ex_library.editions

;
INSERT INTO mart.catalogue_test
SELECT
    id,
    NULL AS pages
FROM src_ex_library.accessions;