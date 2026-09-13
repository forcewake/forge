# Audit and log retention (runbook)

What forge keeps, where, and for how long. Principle: audit data must be
tamper-evident enough to answer "what did the bot do and why", without
becoming a secrets or prompts dump.

## Inventory

| Data | Store | Purpose | Default retention |
|------|-------|---------|-------------------|
| `flow_runs`, `step_runs`, gate rows, outbox, action journal | Postgres | lifecycle + audit of every transition and external write | keep ≥ 180 days; archive before delete |
| `llm_calls` usage ledger | Postgres | every model call incl. failures (tokens, latency, model) | keep ≥ 180 days |
| `event_inbox` rows | Postgres | webhook idempotency | processed rows may be pruned after 14 days |
| Captured webhook payloads (`FORGE_CAPTURE_DIR`) | files | post-mortem material | 14 days recommended; **contains issue/comment text** |
| Redis queue/dedup/heartbeats | Redis | transient | expires by itself; DLQ pruned with the queue |
| CI job traces | GitLab | harness evidence | GitLab's own retention |

## Rules

1. **No secrets in audit data.** CI variable *values*, tokens, and model
   keys never enter Postgres or the capture dir. If a leak is found in a
   capture, delete the file and rotate the secret
   ([token-rotation.md](token-rotation.md)).
2. **Prompts/responses are usage metadata, not content dumps.** The usage
   ledger stores token counts and digests; full model transcripts live in
   the CI traces, under GitLab's retention.
3. Deletion of terminal runs is a **human decision** — `blocked` runs are
   the operator's inbox (M3 operator-recovery flow).

## Pruning commands

```bash
# captured payloads older than 14 days
find data/captured/ -type f -mtime +14 -delete

# processed inbox rows older than 14 days
podman exec forge-postgres psql -U forge -d forge -c \
  "DELETE FROM event_inbox WHERE processed_at IS NOT NULL AND received_at < now() - interval '14 days';"

# usage ledger / run rows: archive first, then delete by date
#   pg_dump --table=llm_calls --data-only ... (archive), then DELETE as above.
```

Wire these into cron/systemd timers on the forge host; verify with
`SELECT count(*) FROM event_inbox;` staying bounded.
