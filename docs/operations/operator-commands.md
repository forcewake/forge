# Operator commands

GitHub and GitLab share one command engine. Signed native events and the
authenticated operator API resolve the configured native repository and
human actor. Current native approver authority is checked before a mutation;
parsing a command grants nothing. Bot-authored notes never become commands.
Repeated native/API command identities replay their original durable result.

| Command | Current effect |
| --- | --- |
| `/implement` | Read the issue and frozen native sources, plan within the reserved allowance and post the exact approval artifact. |
| `/go` | Recheck issue, native plan comment, actor, source and gate; consume approval once before dispatch. |
| `/cancel` | Revoke current authority before cleanup; retain any already-landed native truth. |
| `/retry` | Retry only the original eligible step and immutable evidence; unresolved pause requires exact resume. |
| `/pause` | Persist the pause before capture and fence candidate/publication advancement. |
| `/resume` | Authorize the exact retained checkpoint and matching prior execution identity. |
| `/steer` | Deliver bounded guidance only through a driver that declares the capability. |
| `/status`, `/why-blocked` | Read one bounded, redacted, repeatable durable snapshot without model or native writes. |
| `/reconcile` | Probe existing uncertain effects; a missing response never authorizes a blind replay. |
| `/fix`, `/ask`, `/approve-revision` | Use native feedback and separately authorized correction lineage on the same Draft. |
| `/amend` | Apply an exact bounded budget amendment through its durable command/effect receipts. |

The `forge` label admits planning; removal requests cancellation at the
current authority boundary. An edit before approval invalidates the original
task authorization. A later event cannot silently rewrite approved bytes.

Lane controls require a committed delivery intent before driver I/O. Lost
begin/ACK/native responses remain unknown and are not redelivered after a
restart. A lane acknowledgement does not release an accepted publication
pause; only the exact accepted checkpoint-resume decision does.

Use the exact current parser spelling and arguments; structured operator
API requests bind explicit run and repository identity. See
[configuration](../../forge.example.toml),
[operator evidence](operator-view.md) and
[the command engine](../../src/forge/engine/commands.py).
`/security`, legacy debug/chat commands and compatibility APIs are absent.
