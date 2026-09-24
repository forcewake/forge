# billing-policy

Billing's policy service. READ-ONLY neighbor of orders-api: Orders
imports its decisions over the policy client (`src/policy/client.py`)
and must never re-decide them locally.

The CURRENT refund approval policy (revision F-2024-11) lives in the
final revision block of `src/policy/refunds.py`; the F-2023/F-2024-02
sections above it are superseded history kept only for statement
reconciliation.
