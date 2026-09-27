# R41-10 (#365) — the CURRENT pairing's grant-consumer trace (live)

The gap this cycle closed: the 2026-09-26 redemption record proved the
transport with a CONTROLLED recorder and an OLDER lane package (the
promoted v0.39.0 tree `b521e1a`) against newer control-plane code —
strong transport evidence, not a qualification of the CURRENT
runner-side checks or the CURRENT pairing. This trace re-ran the
sentinel consumer proof with the SELECTED pair the #363 freeze pins and
added the rotation-recovery runbook plus the locator-to-env-slot
preflight.

## The pairing (both halves from the SAME composition)

| Half | Identity | Proof it ran |
| --- | --- | --- |
| Control plane | the CURRENT working tree's alignment build — image `sha256:11c4bb30…`, reports **0.41.0**, schema head **032** | `alignment-receipts.json` (five receipted `align_lab.py` runs) + `qualification/inventory-2026-09-27-redemption-pairing.json` |
| Lane package | the SAME tree's pinned wheel `forge-0.41.0-py3-none-any.whl` @ `sha256:2616d221…` | sha256-verified INSIDE the lane job before install, and the lane's OWN trace echo (`forge lane package version=0.41.0 … sha256=2616d221…`) — the driver asserts that marker, never assumes it |

The shipped SDK-lane template is committed into the disposable project
VERBATIM (byte-identical to the frozen `3d74be37…`) and included BY
local include; the ONE local override is the lane job's `before_script`
install seam — the template's `git+…@${FORGE_LANE_REF}` line swapped
for the sha256-verified wheel ladder (the same ladder the GitHub
template documents for `FORGE_LANE_WHEEL`).

The composition lineage: the #363 freeze (`ab08a317…`) pinned this tree
at wheel `bb8f18a8…`; this cycle added the R41-10 preflight to
`src/forge` (see below), rebuilt the wheel (`bb8f18a8…` →
`2616d221…`) and re-aligned the lab (`ddcb9137…` → `11c4bb30…`,
schema 031→032). The re-freeze binds the new receipts
(`57d3839f…`). The src delta beyond `ab08a317` is EXACTLY the
preflight — minimal, suite-green, reported prominently.

## The arms (all live, ids from `live-run-evidence.json`)

1. **reach** — the runner dialed the recorder (`:8480`) and fetched +
   sha-verified the pinned wheel (`:8481`) before any dispatch (job
   1090).
2. **reject** — the INSTALLED wheel's own response fence on the real
   runner (scratch job 1094): doctored redemption responses (wrong
   attempt / route / ref / slot / binding-revision / expiry /
   operation) all refused typed `credential_redemption_failed` before
   any vendor client exists; the correct document passes; the lane-side
   preflight refuses the misbound locator. The FULL offline matrix
   stays covered by the #342 suites — this arm proves the installed
   bytes carry the fence.
3. **trace** — run `a4d0062b`: no grant before `/go` (asserted) → the
   native dispatch MINTED grant `28560fdf` (attempt 0, binding revision
   1) → the lane bootstrap redeemed (receipt joins grant → redemption
   `bd7f2c70` → attempt → consumer) → the recorder captured EXACTLY
   ONE model call whose Authorization matches the BROKER-selected
   sentinel while the FRESH ambient competitor never appeared.
   Terminal `api_error` is the recorder's honest 400 (zero real model
   calls; `consumer_status` stays `staged-unresolved`).
4. **preflight** — the R41-10 fence in three acts: `bind()` refused
   `env:FORGE_BROKER_MODEL_TOKEN` typed `binding_slot_mismatch`
   (`preflight.binding_slot_mismatch`) at the bind moment; the same
   misbound binding HAND-WRITTEN past the bind seam (the #343 incident
   shape) dispatched at a CURRENT lane — which refused at ITS OWN boot
   preflight with ZERO redemption endpoint calls and zero model calls;
   the corrective rebind (rev 2) recovered the lane (grant join + the
   broker sentinel presented).
5. **rotate** — a same-ref rebind rev 2→3 between the mint of grant
   `fbd2acb4` and the lane's redemption: the ENDPOINT answered **403
   `binding_revision_mismatch`** (naming authorized revision 2 vs live
   3, zero emitted values), the real lane failed CLOSED (recorder
   silent), and the post-rotation grant `a6c776d1` redeemed at
   revision 3 (recovery). Wrong-ref / wrong-route probes refused typed
   (`grant_ref_mismatch` / `grant_route_mismatch`) on the live attempt.
6. **retire** — gen-5's lane followed its own grant `2ace0652`; gen-4's
   lane token refused typed superseded.
7. **expire** — three sub-arms: (A) the 20 s window — the endpoint AND
   the real lane refused typed `grant_expired`; (B) the 240 s window —
   `podman restart forge-app` COLD mid-window with the deadline column
   byte-identical after the restart and an idempotent in-window replay
   (200, grant `0886de18`, redemption `0c03a225`, 222.6 s to deadline)
   through the RESTARTED plane; (C) restart-first at 20 s — the typed
   `grant_expired` refusal through the restarted plane on a LIVE
   attempt whose lane then failed closed.

The ledger at teardown (project 151): 6 runs, 14 authority rows, 8
redeemed ledger rows — every one joined to its grant at its binding
revision; every typed refusal left NO row (0 refused rows exist in the
database).

## The preflight as landed (the src delta — reported prominently)

- `src/forge/adaptive/project_credentials.py` —
  `binding_slot_preflight(provider, credential_ref)` + a typed refusal
  in `ProjectCredentialRegistry.bind` (`binding_slot_mismatch`,
  observability `preflight.binding_slot_mismatch`).
- `src/forge/lane_driver.py` — `lane_binding_slot_preflight(env)`
  invoked in `redeem_lane_credential` BEFORE the redemption HTTP call
  (zero endpoint traffic on a misbound dispatched ref).
- Tests: `tests/test_adaptive_project_credentials.py`
  (`TestBindingSlotPreflight`), `tests/test_operation_grant.py`
  (`TestLaneBindingSlotPreflight`).
- The runbook: `docs/operations/credential-rotation.md` (rotate →
  typed refusals under the old revision → corrective rebind → new
  grant → lane recovers), linked from the supported-profile runbook §8a.

## Live-found (recorded honestly; the driver receipts each)

- uv validates the wheel FILENAME from a PEP 508 direct reference —
  the downloaded artifact must carry its real wheel name (the first
  lane install answered "Must have a Python tag").
- A factory `/retry` REUSES the run's recorded attempt base — a CI fix
  lands only through a FRESH run (two install-miss runs are receipted
  under `install_miss_*` in the bundle and cited in the record's note).
- A `/retry` against a `waiting_harness` run is refused by the
  continuation gate — the lane's terminal report takes a beat to park
  the run (the rotation recovery now waits for the park).
- The endpoint's refusal precedence on a TERMINAL attempt is
  `attempt_terminal` BEFORE `grant_expired` — the expiry probe must
  ride a LIVE attempt; a warm runner parks a `/retry`'s lane inside
  ~90 s (arm C was re-cut restart-first + fresh issue for exactly
  this; three timing findings are receipted under
  `expire_arm_c_first_attempt` / `_second_attempt` / `_third_attempt`).
- The typed `binding_revision_mismatch` refusal IS directly observable
  at the endpoint (403 with the typed detail) — #343 could only read
  it from the app log.

## Spend

Zero real-model calls (the recorder route end-to-end). Six planner
calls / 5466 tokens total on glm-5.3-flash via litellm (two spent on
the honestly-recorded install-miss runs) — the same sub-cent class as
the #343 cycle, well under the $0.15 planner bound. The broker-held
credential itself answers 401 token-expired on real model calls —
irrelevant to this trace (the identity proof is about WHICH value
arrives) and recorded as the known operator item (it also keeps the
deployment-ops model-leg drill blocked).

## Reproduce

```bash
uv run python scripts/align_lab.py --apply \
  --receipts docs/evaluation/2026-09-27-redemption-pairing/alignment-receipts.json \
  --extra-env FORGE_CREDENTIAL_DELIVERY=runner-redemption
uv run python scripts/run_redemption_qualification.py setup
uv run python scripts/run_redemption_qualification.py reach
uv run python scripts/run_redemption_qualification.py reject
uv run python scripts/run_redemption_qualification.py trace
uv run python scripts/run_redemption_qualification.py preflight
uv run python scripts/run_redemption_qualification.py rotate
uv run python scripts/run_redemption_qualification.py retire
# expire needs the window re-alignments (20 → 240 → 20-restart-first → 3600 restore)
uv run python scripts/run_redemption_qualification.py expire
uv run python scripts/run_redemption_qualification.py teardown
```

The recorder and the wheel host run as lab-host containers:
`forge-recorder` (`:8480`, `recorder_server.py` in this directory) and
`forge-wheelhost` (`:8481`, serving the pinned wheel from
`data/lane-wheel/`). Values (sentinels, tokens) live only in the
maintainer-private state under `data/` (gitignored); every published
artifact carries digests, never values.
