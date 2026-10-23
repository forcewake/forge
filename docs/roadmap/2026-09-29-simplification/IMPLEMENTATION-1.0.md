# Local 1.0.0 implementation

Owner direction on 2026-10-23: proceed locally toward the ADR-0035 target,
without waiting for GitHub prerequisites. The requested end state is the
new 1.0.0 architecture, not only a plan or a renamed 0.42 release.

## Binding scope

- Subtract obsolete/test-only qualification and legacy subsystems.
- Separate `forge-protocol`, `forge-lane` and the control plane.
- Replace three provider lifecycles with one engine over narrow ports and
  journaled effects; keep provider-native concurrency/recovery semantics.
- Preserve I1–I8, bot self-trigger protection, reviewed ledger decisions
  and every applicable edge-case pin.
- Implement and verify the local installation, configuration, operator and
  release-artifact surfaces for version 1.0.0.
- Report hosted/live/pilot acceptance separately; local tests are evidence
  of their stated scope. No remote publication was requested.

Use ROADMAP task boundaries and internal dependencies. GitHub publication,
required hosted checks, maintenance-branch publication and recovery-tag
creation do not block local work under the owner's explicit override.
The owner's later instruction stops repeated full runs at each step:
use focused checks while implementing, then verify the assembled version.
Keep the existing gates and tests intact; record actual verification.

## Current work

Working branch: `rewrite/1.0.0`, based on `0bf2a4e`.

| Task | Current state |
| --- | --- |
| P1-01 shared project config | Implemented; 6 red relocation tests before the move, 145 focused passes, 9,575 default passes, 807 invariant passes with no skips; all local guards green |
| P1-02 saga removal | Implemented; obsolete coordinator removed, retained tests ported, actual socket-loss recovery fixed; 289 focused passes and six live-credential skips |
| P1-03 test-only modules | All five clusters implemented; 18 named modules removed; retained contracts use current product seams |
| P1-04 reference twins | Waits for P1-03 and P1-07 under the adopted plan |
| P1-05 boundary registry | Implemented; 901 source lines removed, 15 contracts added, 48 architecture checks retained; 186 focused passes |
| P1-06 closure matrix | Implemented; grading module, generated publication and approved drop suite removed |
| P1-08 release and CI flow | Source implemented; one build, mandatory provider smokes, signed reports and identical-digest promotion; hosted dry run remains unperformed |
| P1-10 lane-only implementation | Source implemented; builtin execution removed and actual project CI checked before paid work; remaining lifecycle-test ports join engine integration |
| P1-13 deferred features | Implemented; MCP server, scanner/triage and standalone CI-debug removed; historical migration schema preserved and /security refuses explicitly |
| P1-11 Azure removal | Implemented; runtime provider routes removed, shared contracts preserved, local `release/0.42.x` preserves `7ed64ba` |
| P2-01 distribution split | Implemented; server, protocol and lane build as separate wheels, install from wheels, and have enforced import boundaries |
| P2-11 CI templates | Implemented; GitHub reusable/target workflows and GitLab OIDC job, verified lane-only requirements, frozen checkout and read-only repository authority; local YAML/actionlint validation only |
| P2-07 Copilot driver | Implemented; pinned native ACP process, explicit token auth, permission boundary and cancellation ledger; real paid smoke unperformed |
| P2-06 Claude driver | Implemented; scoped gateway auth, confined file capabilities, process-group interruption and usage; real paid smoke unperformed |
| P2-05 lane runner | Implemented; authenticated brief, deadline/control supervisor, exact-WIP checkpoint, independent binary/untracked candidate capture and acknowledged reports; focused checks only |
| P2-03 OIDC sessions | Implemented; trusted issuer/JWKS, exact workflow/job binding, durable global replay receipts and cancellation CAS; local JWT/SQLite and disposable PostgreSQL checks only |
| P2-02 wire protocol | Implemented; immutable v1 bodies, bound approval/candidate/checkpoint identities, canonical serialization and exported schemas |

The owner completed review of all 129 required ledger rows on 2026-10-23,
keeping existing dispositions. The source ledger hash and confirmation
are recorded with the local prerequisite evidence.

Evidence: `.forge/verification/rewrite-1.0.0/` for implementation and
`.forge/verification/phase0-20261023/` for the fresh Phase 0 prerequisite
checks, including 807 invariant passes without skips and 20 mutation proofs
over all 18 registered files. These do not claim 1.0.0 completion.
