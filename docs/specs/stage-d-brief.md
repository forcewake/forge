# Stage D brief — CandidateBundle pipeline, trusted publisher, usage receipts (v0.3.0)

Handoff brief. Read first: ADR-0016, docs/specs/contracts-v0.2.md,
docs/research/harness-interfaces.md (driver table + verify-against-binaries
notes), docs/research/durable-execution.md §6 (idempotency keys).

## 1. HarnessDriver protocol (new: src/forge/harnesses/base.py)

```
class HarnessDriver(Protocol):
    id: str                                   # "builtin" | "claude-code" | "codex" | "grok-build" | "opencode"
    def build_script(self, task: DriverTask) -> str            # the CI `script` body
    def parse_events(self, raw_log: str) -> DriverEvents        # normalized events + usage receipt
```

`DriverTask`: workspace paths, brief path, model (from RunSpec), budget
envelope, allowed-tools posture string. Per-driver modules implement the
verified invocation from the research doc (Codex `exec --json`; Claude
`-p --output-format stream-json` + `--setting-sources ''`; Grok `-p
--output-format streaming-json --always-approve`; OpenCode `run` with
JSON-permission config via env). Keep the existing templates as thin
consumers of `build_script` output where practical — migration over
rewrite.

## 2. Proposal-only CI job (F04/F21)

New shared template snippet (consumed by all harness templates):

1. Fetch the frozen attempt base: `git fetch origin` then
   `git checkout <attempt_base_oid>` (detached). NO write credential in the
   environment: `git remote set-url` WITHOUT credentials; push is
   impossible by construction.
2. Run the driver (from RunSpec image/script).
3. Produce the candidate artifact: `git add -A` in a scratch index +
   `git diff --cached --binary <attempt_base_oid>` → write to
   `$CI_PROJECT_DIR/.forge/candidate.diff` plus `candidate.meta.json`
   (attempt_base_oid, driver id, model, exit code, usage receipt from the
   parsed event stream).
4. Export both as CI **artifacts** (GitLab uploads them; forge downloads
   with the bot token via the artifacts API — the lane itself gets no new
   credential).
5. `.forge/` is a control directory: excluded from any future published
   tree; never committed.

The direct-push mode (`FORGE_BOT_TOKEN` in the lane + `git push`) is
removed from the shipped templates; keep one template flagged
`# UNSUPPORTED-FOR-UNTRUSTED-WORKLOADS (lab legacy)` until v0.4, then delete.

## 3. Trusted publisher (new: src/forge/runs/publisher.py)

`publish_candidate(run, bundle) -> WriteOutcome`:

1. Fetch + parse `candidate.diff` (git apply format) into a ChangeSet-equivalent:
   full new contents computed against attempt_base_oid blobs
   (AuthoritativeBlobReader semantics from Stage A — no truncation).
2. Policy validation (reuse `validate_changeset`): denied paths, CI/config
   files, size caps; the net diff base must equal `attempt_base_oid`.
3. Grant check: run not cancelled/dead, spec digest matches, fence valid.
4. Publish via the existing journaled writer (`expected_head` = current
   branch head; operation marker semantics from Stage A).
5. Verify + adopt exactly like today (branch head + evidence).

No-change candidates (`harness_no_changes` today) on a REPAIR attempt: if
the driver exit was `completed` and the diff is empty, treat as "repair has
nothing to change" → block with `repair_no_effect` instead of adopting a
no-op cycle (F20).

## 4. Usage receipts (F22)

- `HarnessUsage`: driver id, model string AS REPORTED, input/cached/output
  token fields with `completeness: exact|aggregate|unknown`, source
  (event types it came from). Store raw parsed events JSON in
  `candidate.meta.json` + a summarized row in `llm_calls` (reuse the table:
  driver/model/route columns added, completeness flag, attempt ids).
- Builtin path fills the same shape from LLMClient. Never add cached to
  inclusive input; unknown stays unknown (never zero).

## 5. Release image + DB lifecycle (F25/F26)

- Containerfile: `uv sync --frozen --no-dev --extra postgres` (asyncpg),
  copy `migrations/` + agents YAML (or package as data); add
  `python -m forge.migrate` entry (wraps `alembic upgrade head`) and a
  clean-image smoke test: boot app against Postgres+Redis containers
  without source mounts; run doctor; run one migration from a v0.1 dump.
- DB lifecycle: `init_db` keeps create-all for fresh dev sqlite only; app
  startup checks a `schema_version` row (new table) against the supported
  version and refuses incompatible; `reset_engine()` becomes async
  `dispose_engine()` with await dispose on shutdown paths; engine cache
  keyed by database_url.

## Acceptance

- Conformance suite (one suite, all drivers): given a recorded event log
  fixture per driver → parse_events yields the same normalized result
  shape; publisher accepts a well-formed candidate and rejects: diff base
  mismatch, CI/config path, oversized, cancelled run, fence mismatch.
- End-to-end (lab): a fresh run's harness job has no write token, produces
  candidate artifacts, and the published MR is identical to the v0.1 flow.
- Clean-image smoke green in CI.
