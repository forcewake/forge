# audit-events

The audit trail service. Orders (and every other money-moving service)
emits refund/checkout events here; audit-events owns the retention and
emission rules for those streams. Consumers must read the rules from
`src/audit/pipeline.py` — a local copy of an audit rule is an audit
finding waiting to happen.
