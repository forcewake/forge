# Release pipeline

The workflow builds the server, protocol and lane wheels once. It installs the
artifacts outside the checkout, runs the required quality and invariant gates,
and builds the server image from those same wheels. The server image contains
native Git and CA certificates; the lane and vendor SDK run only in target CI.
The fresh-schema canary checks explicit installation and startup refusal for an
unsupported schema. Startup never creates or upgrades tables. Offline history
cutover is a separate operator action, not an automatic previous-image upgrade.

The staged RC manifest (`forge.release-candidate/2`) binds the source commit,
image digest, wheel hashes and independent install routes. `requirements_url`
identifies the server dependencies. Only `lane_requirements_url` and
`lane_requirements_sha256` authorize lane installation; that lock contains the
protocol and lane wheels, the selected lane extra and their hashed dependencies.
The manifest also hashes the generated GitHub wrapper, GitLab include and
GitHub JUnit reporter. The GitHub reusable workflow and GitLab raw HTTPS include
both use the full source commit. Promotion never rebuilds these artifacts.

Both native-provider live jobs are mandatory before promotion. Each creates
two sandbox issues, submits an API-issued human command through the configured
signed hook, waits for the displayed plan and sends `/go <full-run-id>` as the
same authorized actor. This proves signed delivery, not provider webhook
registration. The happy scenario must finish at attempt/generation 1/1. The
repair issue explicitly authorizes an initial implementation returning 41,
followed by correction to 42 after the independent `answer() == 42` test fails.
The repair must finish at 2/2 with two distinct, parent-linked candidates. A
driver that skips the first defect does not prove repair and stops promotion.

Receipts independently read current normalized SQL and native evidence:
consumed approval, immutable lane artifact and OIDC execution, exact native
commit tree and parent, the same Draft, independent required CI, bounded closing
review and unchanged target branch. Native code-failure evidence is required:
GitHub uses the exact Actions job's CheckRun JUnit output; GitLab uses the exact
pipeline's native test-report summary and build IDs. Generic failed status or
runner/configuration failure cannot authorize repair. No bot merges or target
settings changes occur. Created issues and Drafts remain for inspection.

Provision a dedicated nonroot Linux x64 runner labelled
`forge-release-sandbox`, with Docker and a public HTTPS endpoint forwarded to
its free localhost port 8420. Target CI must reach that endpoint. The workflow
starts only uniquely named PostgreSQL, app and worker containers. It mounts
private configuration and CA files read-only, mounts owned blobs separately,
runs `forge ... install` once, then starts separate `serve` and `worker`
processes. `/healthz` and `/readyz` must pass against the real guarded HTTPS
gateway. The release job supplies no guard stub or owner database.

Configure these repository inputs before authorizing a release:

- `FORGE_RELEASE_TOKEN`: release-asset and GHCR publication credential.
- `FORGE_RELEASE_SMOKE_ENV`: JSON with exactly `github` and `gitlab` keys.
  Each value is the private bundle below, containing one connection and one
  repository. The TOML uses the current closed runtime schema and real guarded
  gateway settings. The environment contains exactly the secret names declared
  in that TOML; multiline App PEMs remain JSON strings.
- `FORGE_RELEASE_GITHUB_ACTOR_TOKEN` and `FORGE_RELEASE_GITLAB_ACTOR_TOKEN`:
  sandbox human credentials, distinct from the bot, with issue/comment access.
  Each actor must be a current native approver and match exactly one configured
  operator grant. Bot credentials retain their normal narrow scopes.
- `FORGE_RELEASE_GITHUB_SANDBOX` and `FORGE_RELEASE_GITLAB_SANDBOX`: the
  corresponding configured repository **key**, whose native numeric identity
  is declared in its private TOML.

```json
{
  "schema": "forge.release-sandbox/1",
  "runtime_toml": "<complete current TOML>",
  "policy_json": "<exact trusted pricing policy JSON>",
  "certificates": {"native-ca.pem": "<PEM, when required>"},
  "environment": {"EXPLICIT_SECRET_NAME": "<value>"}
}
```

Use `/data/blobs` for `blob_directory` and `/etc/forge/policy.json` for the
gateway policy. Native `tls_ca_path`, when set, names a supplied
`/etc/forge/*.pem`. The preparer pins the two explicit lane requirements fields
to the staged RC and creates its own PostgreSQL URL; no ambient `.env`, Redis,
target CI variables or existing services are used. Private files have mode
0600 and are deleted with this job's containers after the smoke.

Preprovision each target's default branch with the exact independent oracle
bytes in `tools/release/live_pins.py`, without `forge_release_smoke.py`.
Permit writes only to that implementation file and require
`release-smoke-oracle` in both `verification.required_checks` and
`verification_failure_jobs`. The oracle must run unchanged and emit native
JUnit failure evidence. GitHub also declares its trusted
`verification_workflows`, publishes JUnit with the hashed reporter and installs
the exact generated wrapper at `lane_workflow`. GitLab stores the exact hashed
include as `forge-lane.gitlab-include.yml`; its dedicated `.gitlab-ci.yml` uses
JSON (a strict YAML subset), includes only that local file and defines the
oracle job. Its complete document must equal `live_oracle.gitlab()`; the exact
GitHub workflow is `live_oracle.github(manifest)` at
`.github/workflows/forge-release-oracle.yml`. CI model credentials stay in
target CI. Provisioning these files,
credentials and hooks is an explicit owner operation; the smoke changes none
of them.

Only successful quality, canary and both-provider live jobs permit the final
report, SBOM binding, signatures, attestations and promotion of the tested
digest. Failure leaves the RC unpromoted. Local synthetic peers or a local
bundle proof do not establish hosted/provider acceptance. Running the release
workflow, publishing or tagging still requires explicit owner authorization.
