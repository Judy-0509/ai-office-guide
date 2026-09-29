-- Example: mapping/unioning an existing team database into dataplat's standard columns
-- (dataset, metric, entity, region, period, source, value, unit). This is the same view
-- `samples/fake_source.py` creates for tests; see server/README.md and the public guide's
-- 03-dataplat-manual.md for the walkthrough. Copy this pattern for your own tables --
-- dataplat itself never reads tbl_shipments_long / tbl_price_index_wide directly, only
-- whatever view or query you point source.yaml at.

-- tbl_shipments_long is already "long" (one row per observation) but uses the team's own
-- column names -- rename in the SELECT, or leave them and use source.yaml's `rename:` map
-- (this file renames "inst" here but leaves "dept"/"broker" for source.yaml to demonstrate
-- both approaches).
CREATE VIEW v_dataplat_observations AS
SELECT 'shipments' AS dataset, metric, entity, dept AS region, period, inst AS broker,
       qty AS value, '' AS unit
FROM tbl_shipments_long

UNION ALL

-- tbl_price_index_wide is "wide" -- one column per quarter. UNION ALL one SELECT per period
-- column to turn it long; each SELECT picks a fixed period literal and that column's value.
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q1' AS period,
       '' AS broker, q1 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q2' AS period,
       '' AS broker, q2 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q3' AS period,
       '' AS broker, q3 AS value, 'pt' AS unit FROM tbl_price_index_wide
UNION ALL
SELECT 'price_index' AS dataset, '가격지수' AS metric, entity, region, '2024Q4' AS period,
       '' AS broker, q4 AS value, 'pt' AS unit FROM tbl_price_index_wide;

-- source.yaml then reads:
--   db: <path to this sqlite file>
--   view: v_dataplat_observations
--   rename:
--     broker: source
