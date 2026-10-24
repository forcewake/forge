# Local Forge 1.0.0

The owner authorized completion of the local 1.0 rewrite on 2026-10-23,
waived GitHub prerequisites for that work and required a clean break. On
2026-10-24 the owner directed completion of the working product instead of
continuing old-test ports. No remote publication or owner-lab migration was
authorized. All129 flagged/drop ledger rows were reviewed with their
existing dispositions unchanged.

## Delivered runtime

- One provider-neutral lifecycle, durable SQL journal and exact packaged
  schema baseline. No Redis queue, old application entry point, dual-engine
  flag, execution fallback or compatibility API remains.
- Native GitHub App and GitLab project-bot adapters, exact frozen source
  reads, one validated Git publisher, one Draft and read-only reviews.
- Independent `forge-protocol` and `forge-lane` distributions; Claude Code
  and Copilot CLI are the supported drivers. The server imports neither
  the lane nor a vendor SDK. Extra drivers are removed.
- Consume-once approval of the full frozen plan/policy, explicit native
  verification, bounded repair and closing reserves, durable amendments,
  cancellation, exact checkpoint resume and operator controls.
- Native effects are committed before I/O. Complete recovery probes are
  bounded. A lost control response remains durably inflight and cannot
  replay after restart; terminal ACKs require the matching delivery ID.
- Per-connection TLS trust reaches real HTTPS OIDC/JWKS without sharing
  private roots between connections. No certificate bypass is used.
- Closed TOML configuration, explicit installation, read-only schema
  startup, diagnostics, authenticated operator API/metrics and separate
  app/worker processes. Compose uses PostgreSQL and the explicitly
  configured guarded HTTPS gateway.

The retired runtime's exact recoverable source is in
[the manifest](../../archive/runtime-0.42.json). Historical tests and curated
ledger decisions remain in the repository; they do not ship in the runtime.
No deleted service is restored to satisfy an old test import.

## Local evidence

The assembled current runtime completed issue → plan → exact approval →
signed lane artifact → native Git CAS → one Draft → independent native
checks → posted review → `ready_for_human` for both GitHub and GitLab.
These runs used owned Git/SQL resources and synthetic native/model HTTP
peers. They establish local composition, not hosted or paid-provider acceptance.

The actual primary Runner also completed 47 verified HTTPS requests against
the current app and a fresh owned PostgreSQL database, including real HTTPS
JWKS, steering, pause/WIP checkpoint, report acceptance and lost-ACK restart
refusal. Source and installed-wheel probes find no retired runtime modules.

Strict typing covers all 236 production files; all 18 import contracts pass,
with 10 exact protected authority groups and zero ignored imports. All three
dependency graphs pass. Source size debt is zero: no production file exceeds
600 lines and no function exceeds 80. Protocol schemas match their exports.
The wheel installs and initializes/checks its schema outside the checkout.

Final receipts, invariant XML, correction receipts and local artifacts are
under `.forge/verification/rewrite-1.0.0/final-20261024/` and
`.forge/artifacts/1.0.0/`. The original assembled invariant run passed 2172
cases and exposed two outdated exception expectations: native plan-display
validation now refuses earlier. The corrected proofs additionally exercise
the independent atomic SQL gate and pass for both providers. No invariant
or gate was skipped or weakened. The original run's failure record is retained.

## Remaining acceptance boundary

The owner stopped further migration of the historical test corpus. Its
unported old-runtime imports remain visible; the full default/slow CI corpus
is not qualified on this candidate and its gates have not been bypassed.
This is a local working1.0 source/artifact delivery, not a published release,
green hosted CI, an owner-lab deployment or a live provider pilot.

Use [the installation guide](../../onboarding/cold-install-runbook.md) and
[the configuration example](../../../forge.example.toml). Remote push,
merge, tag, publication, GitHub settings and owner operational changes remain
owner-controlled.
