CREATE TABLE mart.catalogue_test AS
SELECT id, pages
FROM src_ex_library.editions;

INSERT INTO mart.catalogue_test (id, pages)
SELECT id, NULL::INTEGER AS pages
FROM src_ex_library.accessions;