# Harness: dotnet-lane (the reproducible .NET recipe, R28-21)

`dotnet-lane` is the complete second-runtime recipe: a .NET 9 SDK lane for
systems with APIs, databases and queues. It implements the SAME
proposal-only candidate contract as every forge harness (ADR-0016) — no
write credential, mechanical commit/push deny in the driver, the candidate
diff as the only deliverable — and adds a **reproducible verification
tail**: the candidate is restored, built and tested from PINNED inputs,
never from whatever the runner happens to have cached.

The agent inside the lane is the same claude-code CLI the `claude-code`
driver runs (same `-p` + stream-json posture, same allow/deny rules). What
makes this a different driver is the LANE: the image, the pins and the
verification contract below.

Template: [`ci/templates/dotnet-lane.gitlab-ci.yml`](../../ci/templates/dotnet-lane.gitlab-ci.yml)
(job `forge-agent-dotnet`, selected when `$FORGE_HARNESS_DRIVER == "dotnet-lane"`).

## Prerequisites (the pinned inputs)

A target repo is dotnet-lane-ready when ALL of these are committed — the
lane refuses to run without them (fail-closed, before the first paid call):

1. **`global.json`** pinning the exact .NET SDK version, e.g.

   ```json
   { "sdk": { "version": "9.0.312", "rollForward": "disable" } }
   ```

   Set `version` to the SDK the pinned image below reports
   (`dotnet --version`) and keep `rollForward: "disable"` — a lane that
   silently rolled forward to a newer SDK is not reproducible.

2. **`nuget.lock.json`** next to every `.csproj` (enable with
   `RestorePackagesWithLockFile` in the project file, commit the generated
   lock). Restore and build run `--locked-mode` and FAIL when project
   files have drifted past the lock — updating a package is a deliberate
   lock refresh, never an in-lane surprise.

3. **The pinned lane image** — the template pins the multi-arch manifest
   by digest, not the moving `9.0` tag:

   ```yaml
   image: mcr.microsoft.com/dotnet/sdk:9.0@sha256:01fabc4758d1d74e39eda700c8463dae6241a61481f973683692ddcb59a5eeb7
   ```

   (digest verified live against MCR on 2026-09-23; refresh it
   deliberately, together with `global.json`, in review.)

4. **Tests that exercise the real system semantics** — acceptance demands
   more than an empty-database pass:
   - seed LEGACY rows and assert migrations transform them (a
     migrate-from-empty pass does not satisfy the criterion);
   - inject duplicate and out-of-order queue messages and assert exactly-once
     business effects (duplicate delivery exposes incorrect semantics);
   - kill the consumer at the commit/ack boundary and assert no lost and no
     duplicated work (the crash-between-write-and-ack window);
   - run databases and brokers via throwaway containers on the runner
     (Testcontainers or a `services:` block) — no production data, no
     production credentials, no deployment permission is ever required.

## Credential model

No Copilot and no OpenAI keys — the .NET lane rides the **forge gateway**,
exactly like the `claude-code` lane:

| Variable | Required | Meaning |
|---|---|---|
| `ANTHROPIC_AUTH_TOKEN` | yes | the harness API key (the customer's, never forge's — ADR-0015 §4) |
| `ANTHROPIC_BASE_URL` | yes | the forge gateway / proxy base URL |
| `FORGE_BOT_READ_TOKEN` | no | read-only PAT; the lane never receives a write token |
| `FORGE_HARNESS_MCP` | no | canonical `mcpServers` JSON (same dialect as claude-code) |

The driver registry
(`forge.runs.harness_selection.DRIVER_CREDENTIAL_VARS`) deliberately
declares **no** per-driver credential variables for `dotnet-lane`: the
gateway pair above is the shared ambient surface the runner already
provisions for claude-shaped lanes, so `forge doctor` has nothing NEW to
require. A missing gateway pair fails the same way it does on the
claude-code lane — at the driver's own auth boundary, visibly.

## What the lane runs

1. `dotnet --version` — resolved THROUGH `global.json`; a mismatched runner
   image fails before the agent starts.
2. The claude-code agent headless (`-p`, stream-json, `--max-turns 200`,
   commit/push mechanically denied, strict MCP). A failed agent classifies
   the lane `failed` but does NOT abort the tail — the audit trail stays.
3. `dotnet restore --locked-mode`
4. `dotnet build --no-restore --locked-mode --configuration Release`
5. `dotnet test --no-build --logger "trx;LogFileName=forge.trx"
   --results-directory .forge/testresults`
6. The candidate diff vs the frozen attempt base, plus the meta JSON with
   the verification block (below).

## The TRX report format the verifier reads

`dotnet test --logger trx` emits the Visual Studio Test Report (XML,
namespace `http://microsoft.com/schemas/VisualStudio/TeamTest/2010`).
The lane parses — and the verifier consumes — exactly this shape:

```xml
<TestRun ...>
  <Results> ... </Results>
  <ResultSummary outcome="Completed|Failed">
    <Counters total="12" executed="12" passed="11" failed="1" error="0"
              timeout="0" aborted="0" inconclusive="0" notExecuted="0"
              passedButRunAborted="0" notRunnable="0" disconnected="0"
              warning="0" completed="0" inProgress="0" pending="0"/>
  </ResultSummary>
</TestRun>
```

It lands in `.forge/candidate.meta.json` (the uploaded artifact) as:

```json
"verification": {
  "kind": "dotnet-trx/1",
  "restore_exit": 0, "build_exit": 0, "test_exit": 1,
  "trx": {
    "file": ".forge/testresults/<id>/forge.trx",
    "sha256": "<digest of the raw TRX bytes>",
    "outcome": "Failed",
    "counters": {"total": 12, "executed": 12, "passed": 11, "failed": 1}
  }
}
```

Notes for verifier authors:

- the three exit fields separate ENVIRONMENT failures (restore/build)
  from TEST failures — an unrestorable dependency tree is infrastructure,
  not a red candidate (issue 180: "include environment failures
  separately");
- `counters` copies the TRX `Counters` attributes verbatim (numeric ones);
  unknown attributes are omitted, never zero-filled;
- `sha256` binds the parsed summary to the exact TRX bytes the SDK wrote;
- the TRX file itself is NOT a separate CI artifact (the candidate
  contract pins the artifact set to the diff + the meta) — the parsed,
  digest-bound block above is the transport.

## Known limitations

- **GitLab only, in this slice.** The driver is registered and the render
  arm exists (`forge.harness_entry.render_driver_script("dotnet-lane", …)`,
  npm pin → SDK check → agent → `dotnet test`), but the GitHub Actions /
  Azure DevOps dispatch surfaces have no dotnet-lane arm yet; the AzDO
  credential matrix intentionally carries no mapping for it.
- **Linux only.** The pinned digest is the multi-arch list; Windows/macOS
  runners are untested with this recipe.
- **One TRX report.** The parser takes the first `forge.trx` under the
  results directory — a multi-assert blend across several test projects
  still produces one merged TRX (`dotnet test` at the solution level
  merges), but bespoke per-project TRX naming is not supported.
- **The database/broker topology is the repo's.** The lane provides the
  pinned SDK and the locked package surface; containers-for-tests are the
  target repo's own test fixtures. The negative/recovery recipes above are
  the contract the tests must implement, not something the lane injects.
- **Usage receipts ride the claude-code stream.** The event log is
  claude-code stream-json, so the universal log filter runs its
  claude-code renderer; there is no .NET-side usage surface.
