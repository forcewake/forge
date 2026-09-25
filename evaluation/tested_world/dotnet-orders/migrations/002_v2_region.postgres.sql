-- 002_v2_region.postgres.sql — the N-1 -> N upgrade on PostgreSQL 16:
-- widens orders with a backfilled, NOT NULL region and adds the
-- CHECK constraint the baseline lacked. Preservation is proven by the
-- harness's fingerprints (row count + per-row sha256 over the
-- preserved columns), and the CONSTRAINT is proven by a probe insert
-- that must fail after the upgrade and succeeded before it.
ALTER TABLE orders ADD COLUMN region TEXT;
UPDATE orders SET region = 'unknown' WHERE region IS NULL;
ALTER TABLE orders ALTER COLUMN region SET NOT NULL;
ALTER TABLE orders ADD CONSTRAINT orders_total_positive CHECK (total > 0);
