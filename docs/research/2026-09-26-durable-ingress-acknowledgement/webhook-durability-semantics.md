# Durable webhook acknowledgement semantics (R41-02 / #357)

Research basis for `68f22b8` item R41-02. Sources read 2026-09-26: the
official Starlette background-task documentation plus five practitioner
references on webhook receiver durability.

## 1. Starlette BackgroundTasks are an after-response, in-process mechanism

Source: <https://www.starlette.io/background/> — "A background task should
be attached to a response, and will run only once the response has been
sent."

That single sentence settles the semantics: the task runs AFTER the client
already holds the 202. Nothing about it is durable — same process, same
event loop, no retry, no lease, no recovery. A SIGKILL between the response
and the task (or during it) loses an ACKNOWLEDGED command. Using it as the
durability mechanism for `/pause`, `/steer` and `/approve-revision` makes
the HTTP acknowledgement a promise the process may not keep — the exact
gap the review's P05 models.

The correct contract (source: the dropless/crmbridge/playcode references —
"commit-then-2xx", "ack after persist, never after process"): the HTTP
response certifies DURABLE RECEIPT, and only that. The durable write
commits BEFORE the acknowledgement leaves; the heavy work happens in a
worker whose failures cost bounded retries, not lost commands.

## 2. The Redis SET-NX dedup marker must not decide correctness

Practitioner consensus (all five references) on caches in front of the
dedup store:

- A seen-set (SETNX + TTL) is acceptable ONLY as a fast-path filter whose
  false positives are impossible — i.e. the key may only be SET AFTER the
  durable record commits ("positive cache after commit").
- Setting the marker BEFORE the database write inverts the guarantee: a
  failed SQL transaction behind a live marker turns later redeliveries
  into successful empty duplicates for the TTL window. The unique index
  only protects duplicates that REACH the database — the review's P02 is
  exactly this schedule.
- Redis may remain as a wake-up channel and latency optimization; its
  loss/delay must change measured latency only, never correctness.

## 3. The canonical receiver pattern (what forge already mostly has)

The references converge on the shape forge's classic path already uses —
which is the strongest argument for the fix: extend it, don't invent.

1. Verify authenticity first (constant-time token compare on raw bytes).
2. ONE transaction: insert the delivery identity under a unique constraint
   + insert the pending scheduled/outbox work. Commit.
3. Acknowledge 2xx only after the commit ("commit-then-2xx"). An exact
   duplicate re-reads the committed record and acks as a duplicate —
   provably referencing durable state, not a volatile marker.
4. Workers lease pending rows (visibility timeout / lease token), process
   idempotently, and complete only while holding the lease.
5. Crash boundaries: before commit → the provider redelivers into a clean
   slate; after commit before ack → redelivery finds the durable identity
   and adds no second work item. Both boundaries deserve tests that kill
   REAL processes, not cancelled asyncio tasks (the review's acceptance
   criterion 7).

## 4. Design consequences for #357

- Classic commands: keep the inbox+scheduled-step transaction as the only
  correctness decision; the Redis marker moves to a POST-commit positive
  cache (or the dedup response performs an authoritative lookup before
  answering "deduplicated").
- Adaptive commands: route the mailbox insertion through the SAME durable
  inbox/scheduled transaction (or commit the mailbox row before the 202);
  the BackgroundTask may remain only for a non-critical wake-up nudge.
- The e2e proof is the ASGI-subprocess kill matrix: SIGKILL at each
  boundary → a fresh worker recovers the command without manual resend.
