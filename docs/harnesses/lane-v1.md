# Lane v1 onboarding

For GitHub, verify the manifest's `github_wrapper_sha256` and copy its
`github_wrapper_url` asset into `.github/workflows/forge-lane.yml` in the target.
The generated wrapper calls the reusable workflow at the full `workflow_commit`
SHA; no moving tag or branch is an immutable pin. The source wrapper template
must be rendered before use. For GitLab, verify and include the exact
`gitlab_include_url` asset, whose remote include uses the same full commit.
The target runner needs an isolated checkout, HTTPS access to Forge and
Python package downloads. The GitLab template currently uses a disposable
Debian container with Node 22 and installs pinned uv to provision Python 3.13.

The control plane supplies the frozen `forge.lane/1` dispatch JSON and the
release manifest's `lane_requirements_url` and `lane_requirements_sha256`.
The requirement file binds the exact lane and protocol wheels plus all
transitive dependencies. Editable installs, source refs and the server
wheel are absent from the v1 lane distribution. Both templates refuse a
missing digest; no unverified installation fallback exists.

GitLab variables are `FORGE_LANE_DISPATCH`, `FORGE_PUBLIC_URL`,
`FORGE_LANE_REQUIREMENTS_URL` and `FORGE_LANE_REQUIREMENTS_SHA256`.
GitHub inputs are `dispatch`, `requirements_url` and `requirements_sha256`.
Use `claude_code` or `copilot_cli`. Claude receives its scoped model-gateway
grant from the lane API. Copilot requires a dedicated Copilot credential:
`FORGE_COPILOT_TOKEN` in GitHub, passed as `copilot_token` to the reusable
workflow, or masked `COPILOT_GITHUB_TOKEN` in GitLab. Repository write
credentials and Forge's server signing key must never be supplied.

The GitHub job requests its OIDC token for the dispatch nonce through the
Actions runtime. GitLab emits `FORGE_OIDC_TOKEN` with the Forge public URL
as audience. The server pins the repository, caller ref, exact workflow
claim or native pipeline/job identity, attempt and generation.

The lane checks out the dispatch base, disables credential persistence
and push, runs `forge-lane`, and uploads through `/lane/v1`. It does not
commit, open a PR or verify its own candidate. Pausing writes a checkpoint
and exits; resume dispatches another generation. The lane's JSON output
reports whether the terminal report was accepted.

These templates are implemented locally. Hosted workflow validation and
real-provider execution have not been performed for this rewrite.
