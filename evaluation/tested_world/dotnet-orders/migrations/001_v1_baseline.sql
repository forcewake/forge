-- 001_v1_baseline.sql — the N-1 baseline schema (portable SQL: the
-- same DDL runs on PostgreSQL and SQLite). The harness seeds the
-- canary rows AFTER this file and BEFORE the N-1 -> N upgrade.
CREATE TABLE orders (
    id          TEXT PRIMARY KEY,
    total       NUMERIC NOT NULL,
    created_at  TEXT NOT NULL
);
