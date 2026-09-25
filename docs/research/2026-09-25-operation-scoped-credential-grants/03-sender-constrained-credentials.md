# Sender-constrained credentials: binding redemption to the runner (2026-09-25)

How a credential response can be cryptographically bound to the *specific*
runner/job identity so a leaked response cannot be replayed elsewhere.
Covers RFC 8471 Token Binding (history and why it died), RFC 8705
mTLS-bound tokens, RFC 9449 DPoP (the live standard), and what the
DPoP-shaped NEXT adapter for forge's redemption endpoint looks like.
Tags: **[documented]** / **[observed]** / **[inference]**.

---

## 1. The problem, precisely

Every bearer credential — forge's redeemed model key included — is
"usable by any party in possession of such a token" until `exp`
[documented — RFC 9449 §1]. Audience restriction (`aud`) helps but the
RFC's own assessment is blunt: "doing so in practice has proven to be
prohibitively cumbersome for many deployments … Sender-constraining
access tokens is a more robust and straightforward mechanism to prevent
such token replay at a different endpoint" [documented — RFC 9449
Objectives]. Sender-constraining = bind the token to a key the legitimate
holder proves possession of on every use.

## 2. RFC 8471 Token Binding (2018) — the TLS-layer attempt

Mechanics [documented — https://datatracker.ietf.org/doc/html/rfc8471]:

- Client generates a per-server long-lived keypair (ideally in a TPM);
  proves possession **on every TLS connection** by signing the TLS
  Exported Keying Material (`EXPORTER-Token-Binding`, 32 bytes).
- "When issuing a security token … a server includes the client's Token
  Binding ID (or its cryptographic hash) in the token. Later on … the
  server verifies that the ID in the token matches the ID of the Token
  Binding established with the client. In the case of a mismatch, the
  server rejects the token."
- Two binding types: `provided_token_binding` (to the server you are
  talking to) and `referred_token_binding` (IdP binds a token for a
  *different* relying party — directly analogous to forge's broker
  minting a credential consumed at a third party).
- Strict rejection rules: "If the token is bound and a Token Binding has
  not been established for the client connection, the server MUST reject
  the token. If the Token Binding ID … does not match … the server MUST
  reject the token."

Why it is not the answer [observed — secondary analyses]: it needed
browser-level TLS-extension support that "failed to gain cross-browser
consensus and were subsequently deprecated by major browser vendors";
the TLS 1.3 variant was never completed. The RFC formally deals with
TLS 1.2. Conclusion for forge: copy the *binding-to-connection* idea, not
the transport-layer mechanism.

## 3. RFC 8705 mTLS-bound access tokens

The other approved sender-constraining mechanism (with DPoP) in FAPI 2.0:
the token carries `cnf: { "x5t#S256": <cert thumbprint> }` and every
call must present the matching client certificate over mTLS
[observed — FAPI 2.0 / comparison articles]. Operationally heavy for CI
runners (cert provisioning) and broken by TLS-terminating proxies unless
fingerprints are propagated. Relevant to forge only if/when the
lane-control channel is already mTLS with per-runner certs — then the
same `cnf` machinery applies with `x5t#S256` instead of `jkt`.

## 4. RFC 9449 DPoP (Sept 2023) — the standard to model

All normative facts below [documented —
https://datatracker.ietf.org/doc/html/rfc9449].

### 4.1 The proof

Per-request JWT in the `DPoP` header. Header: `typ: dpop+jwt` (exact),
`alg` asymmetric only ("MUST NOT be none or … a symmetric algorithm"),
`jwk` = **public** key only ("MUST NOT contain a private key").
Payload: `jti` (unique "during the time window of validity" — ≥96 bits /
UUIDv4), `htm` (HTTP method), `htu` (target URI **without query and
fragment**), `iat`, plus `ath` (base64url SHA-256 of the access token
string) when used with a token, and `nonce` when the server demanded one.

### 4.2 Binding at issuance and use

- The AS "associates the issued access token with the public key from the
  DPoP proof". For JWT access tokens the binding is
  `cnf.jkt` = base64url of the **JWK SHA-256 thumbprint (RFC 7638)** of
  the client's key — the same `cnf` confirmation-claim machinery as mTLS.
- Resource servers verify (12 normative steps): exactly one DPoP header,
  well-formed JWT, all required claims, `typ`, `alg`, signature over the
  embedded `jwk`, no private key in `jwk`, `htm`/`htu` match **this**
  request, nonce matches, `iat` within window, `ath` equals the hash of
  the presented token, and the token's bound key matches the proof's key.
- Downgrade rule: a server supporting both schemes "MUST reject a
  DPoP-bound access token received as a bearer token" — presenting a
  bound token as Bearer is itself a replay signal.
- Refresh tokens for public clients: "MUST be bound to the respective
  public key … the client MUST present a DPoP proof for the same key that
  was used to obtain the refresh token each time that refresh token is
  used."
- Authorization-code interception defense: `dpop_jkt` request parameter
  pins the key at the /authorize step; at token exchange "the
  authorization server computes the JWK Thumbprint … and verifies that it
  matches … If they do not match, it MUST reject the request."

### 4.3 Replay protection — the two mechanisms

- `jti` replay cache scoped to the target URI for the proof's acceptance
  window ("When strictly enforced, such a single-use check provides a very
  strong protection").
- **Server-issued nonces** when the cache is too costly: server returns
  `DPoP-Nonce` (on 200 or on `400 … error code use_dpop_nonce` / `401
  WWW-Authenticate: DPoP error="use_dpop_nonce"`); the client must
  include it in the next proof. Crucial skew note: "servers MAY limit
  DPoP proof lifetimes by using server-provided nonce values containing
  the time at the server rather than comparing the client-supplied `iat` …
  Nonces created in this way yield the same result even in the face of
  arbitrarily large clock skews." (For `iat` itself: MAY accept "the
  reasonably near future (on the order of seconds or minutes)".)

## 5. forge's NEXT adapter: a DPoP-shaped redemption handshake [inference]

Reality check first: the **redeemed model credential is consumed at the
model provider**, which will not verify a DPoP proof. Sender-binding
therefore applies to what forge controls — the redemption call, any
refresh calls, and the lane-control channel — and the model credential
itself keeps only TTL + single-grant limits as its mitigations.

Concretely:

1. **Runner keygen at lane start** (before first control-plane call):
   fresh ES256/Ed25519 keypair; `forge-lane` holds the private key in
   process memory, analogous to a SPIRE workload key ("the private key is
   generated locally and never leaves the workload").
2. **Grant carries `cnf.jkt`**: at dispatch, or at first redemption, the
   control plane records the runner key's thumbprint in the persisted
   grant (the field already exists in the shape proposed in
   [overview.md](overview.md) §A). This is `dpop_jkt`-style pinning: the
   key is declared before/at issuance, and every later presentation must
   prove the same key.
3. **Per-request proofs on lane-channel calls**: `DPoP:`-style header on
   `GET /lane/credentials/redeem` and on refresh — proof over
   `htm`+`htu`+`jti`+`iat`(+`ath` = hash of the lane HMAC token). Server
   checks: bound `jkt` in grant matches proof key; `htu` is exactly the
   redemption endpoint; `jti` unseen in the acceptance window; nonce
   matches when challenged.
4. **Server-timestamp nonces** (not client `iat`) as the primary freshness
   signal — the RFC's clock-skew-proof construction — issued on the
   authenticated channel and required on redemption. This also fences
   replay of a *captured redemption request*: a stolen request + proof
   has a one-`jti` life at one URI.
5. **A leaked redemption *response*** (the raw model key) still works at
   the provider — binding cannot fix that. Mitigations remain: short TTL,
   single logical grant, per-redemption audit, and (topic 4) an absolute
   deadline that no refresh can extend. What binding adds: the attacker
   cannot *obtain* a fresh/refreshed credential with the stolen lane
   token alone, and cannot silently redeem a copied grant elsewhere —
   possession of the runner's private key becomes the differentiator.

Migration note: the grant schema should reserve the `binding` field now
(optional), so enabling DPoP-style proofs later is a server-side flag +
`forge-lane` upgrade, not a schema change. Precedent for gradual
adoption: DPoP itself made the resource-server rejection rules
conditional on token type (bound tokens MUST be refused as Bearer) so
mixed fleets converge safely [documented].

## Sources

- RFC 9449 DPoP (proof structure, cnf.jkt, validation steps, nonces,
  use_dpop_nonce, dpop_jkt, Bearer-rejection, skew notes):
  https://datatracker.ietf.org/doc/html/rfc9449
- RFC 8471 Token Binding v1.0 (EKM signature, bound-token rejection,
  provided/referred types):
  https://datatracker.ietf.org/doc/html/rfc8471
- RFC 8471 fate (browser deprecation, TLS 1.3 never finished) and the
  mTLS-vs-DPoP comparison incl. `x5t#S256`:
  https://wantsvibes.online/article/oauth-21-and-openid-connect-oidc-architecture-token-binding-and-proof-of-possession-dpop
  and https://hivebook.wiki/wiki/dpop-rfc-9449-oauth-2-sender-constrained-tokens-via-proof-of-possession
  and https://pockit.tools/blog/dpop-oauth-token-binding-stolen-tokens-useless-complete-guide
- SPIFFE key-locality precedent (private key never leaves workload):
  https://alatirok.com/spiffe-spire-ai-agents-svid-mtls
