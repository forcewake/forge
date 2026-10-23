# Native budget guard

The gateway image contains LiteLLM 1.106.2, one callback, one model policy and
one native configuration. Its entry point accepts only a listening address and
port. It verifies the packaged bytes, clears inherited routing/configuration
variables and starts the pinned native gateway. No second forwarding server is
involved.

The callback admits only the four chat/messages text routes. Each key has one
model and an explicit non-resetting USD allowance. Before dispatch, the callback
checks the native reservation, approved policy digest, authoritative database
key, expiry and block state, then commits an exact PostgreSQL monetary hold.
Concurrent gateway processes share the same account lock. Native retries and
fallbacks are disabled.

The hold covers the complete physical model input and output context under the
reviewed tariff, including the 1-hour cache-write input surcharge. The included
Haiku policy reserves USD 0.72 per in-flight request. A smaller key cannot make
a request. Unknown models, nonpositive prices, missing output bounds, non-text
content, unpriced tools and fast/priority/geography/compaction features refuse
before dispatch. Standard ephemeral text caching stays within this tariff.

A complete, priced response settles its hold. A lost response, interrupted
stream, missing usage or failed callback keeps the full hold without a TTL.
Restarting the gateway does not refund it. A proven overrun blocks the account
and preserves the higher liability. Native USD is the enforced monetary axis;
Forge separately allocates and accounts for cumulative calls and tokens.

Build the artifact manifest from the exact reviewed config and policy bytes.
The Forge `LiteLLMGateway` adapter requires those policy bytes and the SHA-256
of that manifest through `strict_policy` and `strict_artifact_digest`. The
nonce-bound native readiness response must match the pinned callback/bootstrap
code, policy, artifact, routes and installed version. Derive the expected digest
from the build inputs; do not learn it from the live readiness response.

The native administrator, upstream credential and database URL enter only as
runtime environment variables. Image construction does not need them. Changing
the config, policy or callback requires a new artifact and an explicit new pin.
Existing running gateways are not changed by editing these source files.

The `LiteLLM budget contract` workflow builds the fixture artifact, starts an
isolated HTTP upstream and disposable PostgreSQL, then runs the actual native
HTTP/SDK/callback/database path. It checks both text APIs and streaming modes,
rejected unpriced requests, blocked/expired keys, one dispatch after a lost
response, and the persistent unknown hold after process restart. The fake
upstream has no provider credentials or network route to a real model.
