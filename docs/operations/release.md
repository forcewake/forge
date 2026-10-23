# Release pipeline

The release workflow builds the server, protocol and lane distributions once.
A fresh environment installs those wheels and runs the default suite and the
Invariant Suite with PostgreSQL; invariant skips stop the release. The runtime
image installs the same wheels and locked dependencies, without a source tree.
The image is booted by digest against a disposable database, including the
existing previous-release migration and seeded-data canary.

The workflow stages a unique GitHub prerelease before either live smoke starts.
Its lane requirements file pins every built wheel and all resolved dependencies
by sha256. Current GitHub and GitLab Claude SDK lanes verify that file and the
server wheel, install the set, and export the verified install identity with
the candidate artifacts. GitLab smoke sandboxes must use the current
`ci/templates/claude-sdk-lane.gitlab-ci.yml`; GitHub sandboxes must use the
current `ci/templates/forge-harness.github.yml`.

Both provider smokes are mandatory. Each creates a real issue, sends an
API-issued human comment through signed production ingress, waits for the plan,
approves that exact run, and waits for `ready_for_human`. It independently reads
the provider's Draft PR/MR, current head, successful `release-smoke-oracle` CI
job and lane artifact. The candidate must preserve the independent test file.
The receipt states that ingress uses signed comment delivery: this smoke does
not claim to verify provider webhook registration. No bot merge is attempted.

Provision a dedicated Linux x64 runner labelled `forge-release-sandbox`, with
Docker and a public HTTPS endpoint forwarded to its localhost port 8420. The
endpoint must be reachable from target CI for lane-control calls. That port
must be free before a release. The workflow creates uniquely named database,
Redis, app and worker containers; it never uses an existing lab database or
queue. It removes those containers and temporary credential files afterwards.

Repository configuration required before publishing:

- Secret `FORGE_RELEASE_TOKEN`: release-asset and GHCR write access.
- Secret `FORGE_RELEASE_SMOKE_ENV`: a JSON object of string settings for the
  disposable app and worker. Include real provider bot/App credentials, bot
  names, webhook secrets, approver usernames, real planner/model settings,
  current harness selection, `FORGE_LANE_CONTROL_URL`, and
  `FORGE_RELEASE_SMOKE_GATEWAY_URL`. The latter two point to the public sandbox
  ingress. JSON preserves multiline GitHub PEM keys. Database and Redis URLs
  are always replaced with the new isolated services.
- Secrets `FORGE_RELEASE_GITHUB_ACTOR_TOKEN` and
  `FORGE_RELEASE_GITLAB_ACTOR_TOKEN`: sandbox-only human actor credentials,
  distinct from the bot. The actors must be configured approvers and may
  create issues/comments and manage the sandbox's lane pin variables.
- Variables `FORGE_RELEASE_GITHUB_SANDBOX` (`owner/repository`) and
  `FORGE_RELEASE_GITLAB_SANDBOX` (numeric project ID or project path).

Each sandbox's default branch must contain exactly the independent oracle
bytes defined by `ORACLE` in `tools/release/live_smoke.py`, and CI must run that
test in a job called `release-smoke-oracle`. The issue asks the lane to create
only `forge_release_smoke.py` with `answer()` returning 42. Sandbox CI retains
its own provider/model credentials under the existing proposal-only policy.
The smoke temporarily sets the wheel and requirements URL/hash variables and
restores prior values; a restoration failure stops release promotion. Created
issues and Drafts remain available for inspection. Existing wheel/ref/dev
install overrides must not contradict the explicit RC route.

Only after both live smokes pass does the workflow create the short release
report, bind its SBOM hash, sign the image and report, and attest image and wheel
provenance. It verifies the staged RC assets again, attaches those unchanged
files to the final release and points final image tags at the tested digest.
It never rebuilds during promotion and never overwrites an existing asset.
A failed job leaves an unpromoted RC for inspection.

A local workflow/test pass does not prove hosted release success. Acceptance
still requires the fork release dry run described in ROADMAP P1-08. Running
this workflow, creating a tag or publishing a release requires the owner's
explicit remote-publication authorization.
