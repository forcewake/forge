-- 002_v2_region.sqlite.sql — the same N-1 -> N upgrade on SQLite
-- (the reference dialect): SQLite cannot ALTER a column to NOT NULL,
-- so the table is rebuilt with the widened shape and the CHECK
-- constraint. Preservation semantics are identical to the postgres
-- variant; the harness's fingerprints do not care which ran.
ALTER TABLE orders ADD COLUMN region TEXT;
UPDATE orders SET region = 'unknown' WHERE region IS NULL;
CREATE TABLE orders_v2 (
    id         TEXT PRIMARY KEY,
    total      NUMERIC NOT NULL CHECK (total > 0),
    created_at TEXT NOT NULL,
    region     TEXT NOT NULL
);
INSERT INTO orders_v2 (id, total, created_at, region)
    SELECT id, total, created_at, region FROM orders;
DROP TABLE orders;
ALTER TABLE orders_v2 RENAME TO orders;
