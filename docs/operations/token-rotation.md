# Token and signing-key rotation

Credentials stay in the deployment's private secret store and selected CI
secret carriers, never source, logs or public receipts. Record identities,
revisions and expiry without values. The control-plane publisher credential
never enters the coding lane.

## Provider, webhook and model credentials

For a GitLab bot PAT or GitHub App key/token, keep the same scoped bot
identity and permissions. Stage the replacement, update both consumers,
verify doctor/provider access, then revoke the old credential. Existing
runs remain durable, but a rejected native call can have an unknown effect:
probe before retry. Never grant target-branch merge or bypass to repair auth.

Rotate webhook secrets in forge and each provider hook as one coordinated
change. A single-secret deployment has a delivery gap during mismatched
values; inspect provider delivery failures/retries afterwards. Do not assume
all missed events will be replayed. Ingress authentication never substitutes
for actor approval checks.

Model credential rotation depends on its selected route. Already running
jobs retain their staged environment. Revoking a value can fail the turn;
that is not zero spend. Existing bound-lane grants retain their original
revision/deadline and refuse after a conflicting rebind. Use
[lane rotation](credential-rotation.md), [native locators](native-locators.md)
and value-free [consumer receipts](credential-consumption.md).

## Existing lane-control and operator credentials

`FORGE_LANE_CONTROL_SECRET` still signs the current legacy control/operator
families while their callers migrate. Changing it invalidates existing
tokens unless the particular route explicitly supports a documented overlap.
Drain or cancel/re-dispatch affected work; do not claim overlap from a
restart alone. Canonical operator scope must be reissued explicitly.

Legacy generation-less lane acceptance has a restart-stable bounded
migration window: explicit `FORGE_LEGACY_CREDENTIAL_DEADLINE` (older alias
honored), recorded `FORGE_LANE_LEGACY_TOKEN_START`, or a write-once persisted
anchor under the checkpoint store. Missing/unpersistable or malformed
state refuses legacy acceptance. Never delete or edit the anchor to reopen
it. Drain grandfathered work through authorized re-dispatch; after expiry,
restart cannot revive it. Generation-scoped tokens remain separately checked.

## New lane v1 server signing key

`FORGE_SECRET_KEY` is a separate server-only secret of at least 32 bytes.
It signs short lane sessions and derives a context-separated encryption key
for cached attempt model grants. CI never receives it. There is no fallback
to the legacy shared secret. Rotation invalidates old sessions and makes
old cached grant ciphertext unavailable under the new key; it must not
trigger a second mint for the same unknown/reserved attempt.

Plan a drain or cancellation plus newly authorized dispatch generations,
coordinate app/worker key rollout, and retain the appropriate old key only
in private recovery storage for backups that require it. Restore database
and key identity together; never copy raw model keys into evidence to work
around cache failure. OIDC issuer/workflow/ref bindings and durable nonce/JTI
receipts remain required after key rotation.

See [backup/restore](backup-restore.md) and [upgrade](upgrade.md).
