# Current lane credential rotation

This runbook applies to the existing `runner-redemption` lane, which remains
in use until provider dispatch migrates. Lane v1 OIDC and gateway grants
are separate; never substitute a legacy token or ambient model key there.

## Rotate without widening authority

A bound model credential rotation creates a new registry revision at the
same canonical subject, provider route and slot-named locator. The binding
registry rejects `env:` locators whose name differs from the route's env
slot (`binding_slot_mismatch`). The dispatched lane repeats that check
before redemption, so hand-edited metadata cannot bypass it. For the
Anthropic gateway route, the locator is `env:ANTHROPIC_AUTH_TOKEN`.

Stage the new value in the deployment's secret store and update the
consumers that read it. Separately re-bind through
`ProjectCredentialRegistry.bind` with the operator identity recorded; do
not hand-edit grants or registry history. Value propagation can require
consumer recreation; the registry revision alone contains no secret.
Its locking/TTL semantics are in [consumer receipts](credential-consumption.md).

Run `forge doctor --project <id>` before dispatch. It checks delivery mode,
carrier existence, masked/protected/ref compatibility, redemption endpoint
configuration and correction scope without redeeming a secret. A protected
carrier unavailable on factory refs is a setup failure, not permission to
add an ambient duplicate.

## Existing grants and recovery

An old grant records its original binding revision and absolute redemption
deadline. Rotation does not extend either. Redemption against a different
revision refuses `binding_revision_mismatch`; expired, superseded and terminal
attempts also refuse. No model value is emitted, no ambient fallback occurs,
and the lane records credential failure with zero authorized model turns.
A restart cannot move the deadline. Replayed successful redemption keeps
its original authority and audit linkage.

Recover through the guarded native retry/restart route for a new authorized
attempt generation. The next dispatch resolves the current registry and
mints a fresh grant; old authority remains refused. If the original binding
was wrong, corrective rebind is another revision, not history repair.
Verify that the newest value-free consumer/redemption receipt joins the
new grant, attempt and current binding revision. Never log secret material.

Native secret-name migration follows [native locators](native-locators.md).
Bot/webhook/operator credentials and the separate new lane signing key
follow [token rotation](token-rotation.md). Historical examples remain at
`docs/operations/credential-rotation.md` in the [archive](../archive.md).
