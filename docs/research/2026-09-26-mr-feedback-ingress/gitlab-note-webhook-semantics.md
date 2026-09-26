# GitLab note-webhook semantics for MR feedback commands (R40-01 / #337)

Research basis for `b521e1a` item R40-01 — connecting `/fix` and `/ask`
to the real webhook and reconciler. Sources read 2026-09-26.

## 1. The Note event payload — the identity fields that matter

Source: <https://docs.gitlab.com/user/project/integrations/webhook_events.html>
(§ Comment events / Note trigger; verified against the live lab's
existing note routing in `src/forge/gateway/router.py`).

| What | Field |
| --- | --- |
| The note itself | `object_attributes.id` (unique per project instance) |
| Noteable (MR) | `object_attributes.noteable_id` + the `merge_request` object (`merge_request.id`, `iid`, `last_commit`) |
| Author | `object_attributes.author_id` + the top-level `user` object (`user.id`, `user.username`) |
| Project | `project_id` + `project` object (`project.id`, `path_with_namespace`) |
| Note body | `object_attributes.note` (the `/fix …` text) |
| Discussion thread | `object_attributes.discussion_id` |

Design consequence: the LOGICAL identity of a feedback request is
`(project.id, merge_request.iid, object_attributes.id)` — a note id is
unique inside a project, so redelivery (manual retry, re-POST, worker
restart replay) collapses on that triple. The delivery UUID (§2) is a
transport-level id only: two different deliveries of the same note must
resolve to ONE request (acceptance criterion 4 of #337).

## 2. Delivery headers — what GitLab sends and what it does NOT

Source: the webhook events doc + observed live traffic (the lab's
existing ingress already parses these).

- `X-Gitlab-Event` — the event kind (the note trigger is `Note Hook`).
- `X-Gitlab-Token` — the shared secret, compared **constant-time**.
  GitLab does **NOT sign the body** (no HMAC): the security boundary is
  the token alone. Consequence: never trust the payload's claimed
  identity beyond the token check, and keep the constant-time compare.
- `X-Gitlab-Event-UUID` — a **per-delivery** identifier. Useful as an
  ingress-level idempotency key for exact network replays, but NOT the
  logical note identity — a manual redelivery from the UI can carry a
  new UUID for the same note event.

## 3. Delivery reliability semantics — why the inbox pattern is mandatory

Sources: GitLab docs + <https://hookdeck.com/webhooks/platforms/how-to-solve-gitlab-automatic-webhook-disabling>
+ <https://about.gitlab.com/blog/gitlab-webhooks-get-smarter-with-self-healing-capabilities>
+ <https://svix.com/resources/webhook-reviews/gitlab-webhook-review>.

- **Timeout: 10 seconds.** A slow handler IS a failed delivery.
- A failure is any 4xx, any 5xx, a timeout, or a connection error.
- **Auto-disable: 4 consecutive failures** → backoff 1 min, doubling,
  up to 24 h. **40 consecutive failures** → permanently disabled.
- Since GitLab 17.11 all error classes self-heal after backoff (older
  versions permanently disabled on 4xx).
- Retries on 5xx are attempted for up to 24 h (self-managed docs label
  this not production-ready); **a per-event retry queue does NOT
  exist** — a single failed delivery can be permanently lost.

Design consequences for #337 (all already the forge posture — keep them):

1. **Ack fast, process async**: the ASGI ingress persists the inbox row
   and 200s; the durable worker + reconciler do the work. Never do paid
   or slow work in the webhook handler.
2. **Dedup at TWO layers**: transport (delivery UUID at ingress — exact
   network replay) AND logical (project+MR+note id at request creation —
   manual redelivery, worker restart replay). One logical request, one
   decision identity, at most one authorized correction start.
3. **Refusals answer 200 with a typed payload** where possible: a 4xx
   spike from malformed commands would count toward hook auto-disable
   (4 → backoff → 24 h of LOST events project-wide). A malformed `/fix`
   must be a recorded refusal, not an ingress error status.
4. **Lost deliveries are expected**: the reconciler's polling pass (the
   existing `evaluate_review_corrections` registration point) is the
   recovery path for a note whose inbox write succeeded but processing
   died — that is exactly why the review demands the reconciler
   registration, not just the ingress.

## 4. Subject binding

The payload carries the project identity — bind the request to the
provider connection + repository + MR + note + discussion + observed
head (from `merge_request.last_commit.sha`). Numeric ids repeat across
GitLab instances: `(connection, project.id, mr.iid, note.id)` is the
minimal collision-free key; the review's negative test 2 (same numeric
ids under two repositories) demands the connection/repo axis be part of
every lookup, never inferred from the id alone.
