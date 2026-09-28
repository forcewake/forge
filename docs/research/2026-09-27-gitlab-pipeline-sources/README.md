# GitLab pipeline `source` values (R42-02 / occupancy classification)

Sources read 2026-09-27: the official merge-request-pipelines documentation
(https://docs.gitlab.com/ci/pipelines/merge_request_pipelines/) and the
CI_PIPELINE_SOURCE value inventory.

## The canonical values

`Pipeline.source` (the REST field / `CI_PIPELINE_SOURCE` variable) takes
exactly these 15 documented values:

push · merge_request_event · api · chat · external ·
external_pull_request_event · ondemand_dast_scan · ondemand_dast_validation ·
parent_pipeline · pipeline · schedule · security_orchestration_policy ·
trigger · web · webide

**The MR pipeline source is `merge_request_event`** — there is no
`merge_request` source value. forge's `_OCCUPANCY_NON_CODING_SOURCES`
constant (src/forge/runs/service.py:366) matched `"merge_request"` — a
value GitLab never emits for this purpose, so the verification-pipeline
exclusion silently never matched. Normalize only at the adapter boundary;
test captured payloads, not presentation labels.

## Two doc facts that matter to forge

1. MR pipelines run "on the contents of the source branch only and ignore
   the content of the target branch" — a source-branch pipeline listing
   under a factory branch is expected shape, not an anomaly.
2. The docs state MR pipelines "do not have access to protected variables
   or protected runners" — the exact mechanism behind the #364 live-found
   failure (protected CI variables did not reach the unprotected factory
   ref; the carrier had to be provisioned masked-not-protected). The
   credential preflight (R42-03) should encode this as a compatibility
   rule: protected carriers require protected refs (or masked provisioning).
