# Actions artifacts + usage receipts (R16 / R23 research)

Research for review findings R16 (P1, artifact upload/validation gaps) and R23 (P2, usage
receipt identity + normalization + idempotent ingestion) against the GitHub Actions lane
(`ci/templates/forge-harness.github.yml`) and its control-plane consumers
(`src/forge/execution/github_actions.py`, `src/forge/runs/backends.py`,
`src/forge/runs/candidate.py`).

Every load-bearing claim carries a URL. Facts verified against the live docs/READMEs on
2026-09-17.

---

## Artifact v4 ground truths (URLs)

### Hidden files: default exclusion since v4.4.0 (the R16 root cause)

- `actions/upload-artifact` v4.4.0 (August 2024) made hidden files **excluded by default**:
  "We will no longer include hidden files and folders by default in the upload-artifact
  action of this version", rationale "This reduces the risk that credentials are
  accidentally uploaded into artifacts"; the escape hatch is the new
  `include-hidden-files` input ([release notes v4.4.0](https://github.com/actions/upload-artifact/releases/tag/v4.4.0),
  PR "Exclude hidden files by default" [#598](https://github.com/actions/upload-artifact/pull/598)).
- The change was announced in [issue #602](https://github.com/actions/upload-artifact/issues/602)
  (effective 2024-09-02; backported to v3 via #604).
- Definition, from the [upload-artifact README](https://github.com/actions/upload-artifact):
  "Hidden files are defined as any file beginning with `.` or files within folders beginning
  with `.`". On Windows, OS-hidden-attribute files are not treated as hidden "unless they
  have the `.` prefix". The `include-hidden-files` input defaults to `'false'`.
- Consequence for forge: both uploaded paths live under `.forge/` (a dot-directory), so the
  default search finds **zero** files. Because the template sets `if-no-files-found: error`
  (line 190 of `ci/templates/forge-harness.github.yml`), the lane does not silently upload an
  empty archive — it fails the "Upload candidate" step, which downstream reads as an
  infrastructure-class lane failure rather than a code failure. Either way the lane is
  unusable until the flag (or a staging copy) is added.

### Name uniqueness + immutability

- "Artifact names must be unique since each created artifact is idempotent so multiple jobs
  cannot modify the same artifact"; "Artifacts created by upload-artifact@v4 are immutable";
  with default `overwrite: false` the action "will fail if an artifact for the given name
  already exists"; `overwrite: true` gives the artifact a **new ID** and the previous one no
  longer exists. Uploading to the same artifact name from multiple jobs in one run is not
  supported (all quotes: [upload-artifact README](https://github.com/actions/upload-artifact)).
- The v4 backend rationale, from the GitHub blog: "we scoped all the artifact content to a
  single archive zip on upload", "Once an artifact is uploaded cannot be altered", "there
  cannot be multiple v4 artifacts with the same name, in the same workflow run"
  ([Get started with v4 of GitHub Actions Artifacts](https://github.blog/news-insights/product-news/get-started-with-v4-of-github-actions-artifacts/)).
  Forge's per-attempt name `forge-candidate-${{ inputs.run_id }}` is unique per forge run but
  **collides across attempts** if the same forge run re-dispatches into the same GitHub run;
  with `concurrency.cancel-in-progress` superseding attempts, prefer
  `forge-candidate-${{ inputs.run_id }}-${{ github.run_id }}-${{ github.run_attempt }}` or
  accept the "already exists" failure as an attempt-supersession signal.

### Size, count, retention

- Per-job cap: "there is a limit of 500 artifacts that can be created for that job"
  ([upload-artifact README](https://github.com/actions/upload-artifact)).
- **No per-artifact byte cap is documented.** The binding constraints are the shared
  storage quota (GitHub Free 500 MB, Team 2 GB, Enterprise Cloud 50 GB; "GitHub Support
  cannot increase storage limits for GitHub Actions") and the retention cap
  ([Actions limits](https://docs.github.com/en/actions/reference/limits)).
- Retention: `retention-days` "Minimum 1 day. Maximum 90 days unless changed from the
  repository settings page"; default follows repo settings ("Artifacts are retained for
  90 days by default") ([upload-artifact README](https://github.com/actions/upload-artifact)).
  Forge's 7 days is well inside the cap. Quota accounting is refreshed "every 6-12 hours",
  so quota-exceeded upload failures can lag reality.
- The artifact's displayed "size is the size of the zip that upload-artifact creates during
  upload" and the artifact `digest` output is "the SHA256 digest of the artifact being
  uploaded" ([upload-artifact README](https://github.com/actions/upload-artifact); digest
  also described in [GitHub docs](https://docs.github.com/en/actions/tutorials/store-and-share-data)).

### Zip format + path layout inside the archive

- Server-side, each v4 artifact is a **single zip** assembled by the runner and uploaded as
  one blob ("The runner will assemble the zip archive in memory"; downloads are "a direct
  download from blob") ([v4 blog post](https://github.blog/news-insights/product-news/get-started-with-v4-of-github-actions-artifacts/)).
- Zip entry paths are the searched files relative to the **least common ancestor of the
  search paths**: with multiple paths the action logs "Multiple search paths detected.
  Calculating the least common ancestor of all paths" and uses that ancestor as the archive
  root ([upload-artifact README, "Upload using Multiple Paths"](https://github.com/actions/upload-artifact);
  log text also visible in [nektos/act#1687](https://github.com/nektos/act/issues/1687)).
  For forge's two paths (`.forge/candidate.diff`, `.forge/candidate.meta.json`) the LCA is
  `.forge/`, so the entries are `candidate.diff` + `candidate.meta.json` **without** the
  `.forge/` prefix — this matches the basename-matching already implemented in
  `_extract_candidate` (`src/forge/execution/github_actions.py`).
- Uploaded file permissions are not preserved: "all directories become 755 and all files
  become 644" ([upload-artifact README](https://github.com/actions/upload-artifact)) —
  irrelevant for forge (diff + JSON), worth remembering if we ever ship executables.
- New in 2026: non-zipped single-file artifacts were announced
  ([changelog 2026-02-26](https://github.blog/changelog/2026-02-26-github-actions-now-supports-uploading-and-downloading-non-zipped-artifacts/));
  the current README exposes an `archive: zip|false` input
  ([upload-artifact README](https://github.com/actions/upload-artifact)). Note the README on
  `main` now documents majors beyond v4 — see Open questions on pinning.

### download-artifact@v4: cross-run, extraction layout, error modes

- Cross-run/cross-repo download inputs: `github-token` is "required when downloading
  artifacts from a different repository or from a different workflow run"; `run-id` defaults
  to `${{ github.run_id }}`, `repository` to `${{ github.repository }}`; default scoping
  "can only download Artifacts within the current workflow run"
  ([download-artifact README](https://github.com/actions/download-artifact)).
- Required token permission for cross-run reads is `actions: read`
  ([GitHub docs: store and share data](https://docs.github.com/en/actions/tutorials/store-and-share-data);
  the README example comments "token with actions:read permissions on target repo").
  Forge's control plane downloads with a PAT (outside Actions), so this constraint applies
  to any future in-lane verification job, not to the executor.
- Extraction layout: if `name` is unspecified "all artifacts for the run are downloaded" and
  "by default a directory denoted by the artifacts name will be created for each individual
  artifact"; a **single** artifact downloaded by name/ID is extracted directly into `path`
  with no wrapper directory; `merge-multiple: true` flattens several artifacts into `path`
  ([download-artifact README](https://github.com/actions/download-artifact)).
- Integrity: on download the action recomputes the artifact SHA256 and validates it against
  the recorded digest; `digest-mismatch` handling defaults to `error`
  ([download-artifact README](https://github.com/actions/download-artifact);
  [GitHub docs](https://docs.github.com/en/actions/tutorials/store-and-share-data)).
  Forge's control-plane download bypasses this action, so it must do its own digest check
  against the REST `digest` field.
- Error modes: a not-found name fails the action; an expired artifact surfaces at the REST
  layer as HTTP 410 (below). `skip-decompress: true` is available if a control job ever
  wants the raw zip ([download-artifact README](https://github.com/actions/download-artifact)).

---

## REST API facts

Reference page: [REST API — Actions artifacts](https://docs.github.com/en/rest/actions/artifacts?apiVersion=2022-11-28).

- `GET /repos/{owner}/{repo}/actions/runs/{run_id}/artifacts` — paginated (`per_page` max
  100), supports a `name` query filter and `direction`. Response: `total_count` +
  `artifacts[]` where each artifact has `id`, `node_id`, `name`, `size_in_bytes` (zip size),
  `url`, `archive_download_url`, `expired` (boolean), `created_at`, `expires_at`,
  `updated_at`, `digest` (string or null), and `workflow_run` (`id`, `head_sha`,
  `head_branch`, ...). The `workflow_run.head_sha` + `digest` + `size_in_bytes` triple is
  what forge should assert before trusting a zip.
- `GET /repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip` (the only valid
  `:archive_format`) — "Gets a redirect URL to download an archive for a repository. This
  URL expires after 1 minute"; respond codes **302** (follow `Location:`) and **410 Gone**
  for an expired artifact. So: list → take `archive_download_url` → follow redirect quickly.
- `GET /repos/{owner}/{repo}/actions/artifacts/{artifact_id}` returns the single-artifact
  object (same schema as above); `DELETE` returns 204 (useful for canary cleanup).
- No artifact-specific rate costs are documented beyond the standard REST rate limits; a
  canary making <10 calls is irrelevant volume.
- Contrast with the v4 backend: the REST zip **is** the single server-side zip (same blob
  the UI downloads); the `digest` field shown in the UI/REST is the SHA256 of that zip, and
  `size_in_bytes` is the zip size, not the sum of entry sizes
  ([v4 blog](https://github.blog/news-insights/product-news/get-started-with-v4-of-github-actions-artifacts/),
  [upload-artifact README](https://github.com/actions/upload-artifact)).

---

## Live smoke recipe

Minimal end-to-end canary proving upload → REST download → validation **for hidden-path
files**, runnable in a scratch repo (this becomes the R30 canary). Two uploads per run: one
with today's template posture (expected to fail — proves the default bites), one fixed.

```yaml
name: forge-canary-artifacts
on:
  workflow_dispatch: {}

permissions:
  contents: read

jobs:
  probe:
    runs-on: ubuntu-latest
    strategy:
      matrix: { hidden: ["default", "include-hidden"] }
      fail-fast: false
    steps:
      - run: |
          mkdir -p .forge
          head -c 512 /dev/urandom > .forge/candidate.diff
          printf '{"schema_version":2,"canary":true}' > .forge/candidate.meta.json
          # decoy non-hidden file: proves the job only differs by the flag
          echo control > candidate.diff

      - name: Upload (matrix)
        uses: actions/upload-artifact@v4
        with:
          name: forge-canary-${{ matrix.hidden }}
          path: |
            .forge/candidate.diff
            .forge/candidate.meta.json
          if-no-files-found: error
          retention-days: 1
          include-hidden-files: ${{ matrix.hidden == 'include-hidden' }}

      # Only reachable when the upload succeeded
      - name: Round-trip assert (in-lane)
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          dl=$(mktemp -d)
          # 1) download-artifact extraction: single named artifact -> files land in $dl
          ls "$dl"/candidate.diff "$dl"/candidate.meta.json   # hidden files made it
          # 2) zip-level assert via REST zip endpoint
          artifacts=$(curl -sf -H "Authorization: Bearer $GH_TOKEN" \
            "https://api.github.com/repos/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID/artifacts?name=forge-canary-${{ matrix.hidden }}")
          echo "$artifacts" | jq -e '.total_count == 1 and .artifacts[0].expired == false and .artifacts[0].size_in_bytes > 0'
          url=$(echo "$artifacts" | jq -r '.artifacts[0].archive_download_url')
          # redirect URL expires in 60s -> download immediately; sha256 == .digest field
          curl -sfL -H "Authorization: Bearer $GH_TOKEN" -o /tmp/canary.zip "$url"
          echo "$artifacts" | jq -r '.artifacts[0].digest' | grep "$(sha256sum /tmp/canary.zip | cut -d' ' -f1)"
          python3 - <<'PY'
          import zipfile
          z = zipfile.ZipFile("/tmp/canary.zip")
          assert z.testzip() is None, "CRC failure"
          names = z.namelist()
          assert any(n.endswith("candidate.diff") for n in names), names
          assert any(n.endswith("candidate.meta.json") for n in names), names
          # LCA rule: entries are relative to .forge -> no ".forge/" prefix expected
          assert all(not n.startswith(".forge/") for n in names), names
          PY

  # Negative control: same inputs, no flag -> "Upload (matrix)" step must FAIL here.
  expect-missing:
    runs-on: ubuntu-latest
    steps:
      - run: mkdir -p .forge && echo x > .forge/candidate.diff
      - uses: actions/upload-artifact@v4
        continue-on-error: true
        id: up
        with: { name: forge-canary-neg, path: .forge/candidate.diff, if-no-files-found: error }
      - run: |
          [ "${{ steps.up.outcome }}" = "failure" ] || { echo "hidden default regressed: upload succeeded"; exit 1; }
```

Control-plane sequence (forge executor posture: PAT with `actions: read`, outside Actions):

```bash
RUN=<run_id>                                    # dispatch response or gh run list
ART=$(curl -sf -H "Authorization: Bearer $PAT" \
  "https://api.github.com/repos/OWNER/REPO/actions/runs/$RUN/artifacts?name=forge-canary-include-hidden")
# bind checks before download: not expired, sane size, zip sha256 binding
jq -e '.total_count==1, .artifacts[0].expired==false, .artifacts[0].size_in_bytes<20000000' <<<"$ART"
curl -sfL -H "Authorization: Bearer $PAT" -o canary.zip \
  "$(jq -r '.artifacts[0].archive_download_url' <<<"$ART")"    # 302 -> 60s signed URL
# negative: unknown name -> total_count 0; expired/deleted artifact -> HTTP 410 on the zip route
```

Assertions that "the hidden files made it": (a) `test -f` after download-artifact; (b) zip
`namelist()` contains the two entries with nonzero `file_size`; (c) extracted
`candidate.diff` byte-equals the generated random blob (`cmp`), which rules out a stale
artifact being served; (d) recomputed zip SHA256 equals the API `digest`.

---

## Usage normalization table

Provider fields (all quoted from the linked references):

- **z.ai GLM (OpenAI-compatible, `https://api.z.ai/api/paas/v4/chat/completions`)**: usage
  has `prompt_tokens` ("Number of tokens in user input"), `completion_tokens` ("Number of
  output tokens"), `total_tokens`, and `prompt_tokens_details.cached_tokens` ("Number of
  tokens served from cache"). There is **no cache-write counter and no itemized reasoning
  counter**; GLM-4.5+ returns reasoning as a separate `reasoning_content` channel, so
  reasoning tokens are not subtractable from `completion_tokens`
  ([z.ai chat-completion reference](https://docs.z.ai/api-reference/llm/chat-completion)).
  The endpoint is OpenAI-SDK-compatible ("you can use existing OpenAI SDK code and
  seamlessly switch to Z.AI's model services",
  [z.ai OpenAI SDK guide](https://docs.z.ai/guides/develop/openai/python)). Community/model-card
  reports add that GLM-4.6's `completion_tokens` may already include reasoning tokens
  ([OpenRouter GLM-4.6](https://openrouter.ai/z-ai/glm-4.6)) — treat as inclusive.
- **OpenAI**: `usage.prompt_tokens_details.cached_tokens` and
  `usage.completion_tokens_details.reasoning_tokens` are **breakdowns inside** the inclusive
  `prompt_tokens` / `completion_tokens` totals — `max_completion_tokens` is documented as an
  upper bound "including visible output tokens and reasoning tokens"
  ([Chat API reference](https://platform.openai.com/docs/api-reference/chat/object)), and the
  help center states "Reasoning tokens are not visible as answer text, but they count toward
  output usage and are billed as output tokens"
  ([Understanding and counting tokens](https://help.openai.com/en/articles/4936856-understanding-and-counting-tokens)).
  There is no cache-write counter in OpenAI usage.
- **Anthropic (and z.ai's Anthropic-compatible endpoint
  `https://api.z.ai/api/anthropic`, used by the claude-code driver —
  [z.ai devpack quick start](https://docs.z.ai/devpack/quick-start))**: usage carries
  `input_tokens`, `output_tokens`, `cache_creation_input_tokens` ("Number of tokens written
  to the cache when creating a new entry"), `cache_read_input_tokens` ("Number of tokens
  retrieved from the cache for this request"), and a `cache_creation` object with
  `ephemeral_5m_input_tokens` / `ephemeral_1h_input_tokens` whose sum equals
  `cache_creation_input_tokens` ([Messages API reference](https://platform.claude.com/docs/en/api/messages),
  [Prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)).

Normalization table (`usage.raw` keeps the provider JSON verbatim; canonical fields are
derived, never mutated in place):

| Canonical field | OpenAI-compat (z.ai, OpenAI) | Anthropic(-compat) | Inclusive/additive semantics |
|---|---|---|---|
| `input_tokens` (non-cached) | `prompt_tokens - prompt_tokens_details.cached_tokens` | `input_tokens` | OpenAI: subtract (cached ⊂ prompt). Anthropic: `input_tokens` already excludes cache ("tokens which were not read from or used to create a cache") |
| `cache_read_tokens` | `prompt_tokens_details.cached_tokens` | `cache_read_input_tokens` | Additive w.r.t. `input_tokens`; billed 0.1x base input |
| `cache_write_tokens` | **absent** → unknown, never 0 | `cache_creation_input_tokens` (=`cache_creation.ephemeral_5m + ephemeral_1h`) | Additive; billed 1.25x (5m) / 2x (1h) base input |
| `output_tokens` | `completion_tokens` | `output_tokens` | Inclusive of reasoning on both OpenAI and GLM-4.6; reasoning, when itemized, is a breakdown — **never add it on top** |
| `reasoning_tokens` (informational) | `completion_tokens_details.reasoning_tokens` if present; else unknown | `output_tokens_details.thinking_tokens` if present (≤ `output_tokens`) | Inclusive subset, informational only |
| `total_tokens` | `total_tokens` if sane, else compute `prompt+completion` | compute `input + cache_read + cache_write + output` | Anthropic total-input formula: `cache_read_input_tokens + cache_creation_input_tokens + input_tokens` ([caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)) |

Rules the ingest must keep:

1. **Never fold cache into input, never add breakdowns to their totals** — the two classic
   double-count bugs. Forge's `HarnessUsage` already encodes the "unknown stays unknown,
   never zero" half of this
   (`src/forge/runs/candidate.py`: "Token fields are ``None`` when unknown — unknown stays
   unknown, never zero, and cached tokens are never folded into the input count").
2. **Unknown/incomplete receipts preserve the raw counters** under `usage.raw` (provider
   JSON as received), set `completeness: "unknown"`, and still record identity fields so
   spend can be reconstructed later if the provider dashboard becomes queryable.
3. **Provider shape is decided by endpoint, not hostname**: the claude-code driver against
   `api.z.ai/api/anthropic` emits Anthropic-shaped counters (cache_write present); the
   z.ai OpenAI-compatible shape has cache-read only. Detect by field presence.
4. Provider quirks worth a comment: xAI counts reasoning **exclusively** (not inside
   `completion_tokens`), unlike OpenAI/Azure
   ([comparison](https://dev.to/maximsaplin/grok-3-api-reasoning-tokens-are-counted-differently-197))
   — the grok-build driver must not subtract.

---

## Recommendations for forge (R16/R23)

Files in play: `ci/templates/forge-harness.github.yml`, `src/forge/execution/github_actions.py`,
`src/forge/runs/backends.py`, `src/forge/runs/candidate.py`, `src/forge/harness_entry.py`.

### R16 — artifact contract hardening

1. **Upload fix (one line, minimal risk)**: add `include-hidden-files: true` to the
   "Upload candidate" step. The `path:` allowlist is already two exact files under `.forge/`,
   so enabling hidden-file upload cannot balloon scope the way `path: .` would; the
   v4.4.0 security rationale (credential leakage via broad globs) does not apply
   ([v4.4.0 notes](https://github.com/actions/upload-artifact/releases/tag/v4.4.0)).
   Alternative considered and rejected as primary: copy the two files to
   `.forge-candidate/` (non-hidden) and upload that. It works on every action version and
   dodges the flag entirely, but changes the zip layout and needs the same LCA reasoning
   anyway — keep it as the fallback if we ever must support pre-4.4.0 runners.
2. **Artifact name**: make it collision-free per GitHub run:
   `forge-candidate-${{ inputs.run_id }}-a${{ github.run_attempt }}` — v4 immutability
   ("there cannot be multiple v4 artifacts with the same name, in the same workflow run",
   [v4 blog](https://github.blog/news-insights/product-news/get-started-with-v4-of-github-actions-artifacts/))
   means a re-dispatch that lands on the same GitHub run currently fails at upload; encoding
   the attempt turns supersession into a distinct artifact and lets the control plane
   prefer the newest `workflow_run`-bound artifact.
3. **`candidate.meta.json` schema v2** (written by the "Emit candidate artifact" step, which
   today emits no `schema_version`, no GitHub-side ids, no spec binding, and never embeds
   the entrypoint's `.forge/usage.json`):

```json
{
  "schema_version": 2,
  "execution_id": "<inputs.run_id>",
  "attempt_id": "<github.run_id>:<github.run_attempt>",
  "spec_digest": "sha256:<hex of brief/RunSpec>",
  "attempt_base_oid": "<inputs.attempt_base_oid>",
  "driver": "claude-code",
  "driver_version": "<pinned CLI version from HarnessDriver>",
  "model": "<inputs.model>",
  "exit": "completed|failed|unknown",
  "usage": { "input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
             "cache_write_tokens": null, "reasoning_tokens": null,
             "completeness": "aggregate", "source": "claude-code:stream",
             "receipts": [{"receipt_id": "...", "driver": "...", "raw": {...}}],
             "raw": "<verbatim provider/event-stream counters>" },
  "manifest_digest": "sha256:<hex of candidate.diff bytes>"
}
```

   Rationale per field: `schema_version` gates parsing (backends reject `>=3` majors, accept
   unknown minors); `attempt_id` (GitHub's own `run_id:attempt`) + `execution_id` give the
   receipt its identity for R23; `spec_digest` binds the lane to the approved RunSpec;
   `manifest_digest` lets the control plane verify the diff was not substituted inside the
   zip; `usage` nests exactly where `HarnessUsage.from_meta` already looks
   (`meta["usage"]`), so the existing parser keeps working with v1 fallback.
   `harness_entry.py` should emit the receipt object to stdout or a file path the meta step
   can inline (it already aggregates to `.forge/usage.json`; wire it in the same heredoc).
4. **Download-side zip validation recipe** (in `_extract_candidate` /
   `github_actions.py`, before any parse):
   - pre-zip REST checks: `expired == false`, `size_in_bytes` under a compressed cap
     (suggest 20 MB), `digest` present and re-verified after download (SHA256), and
     `workflow_run.head_sha` equals the commit the run was dispatched on;
   - post-zip structural checks: `zipfile.ZipFile.testzip()` (CRC), entry count ≤ 3,
     every entry name matches the allowlist basenames **exactly once** — today
     `next((n for n in names ...))` silently picks the first basename match, so a
     crafted/duplicate entry would shadow the real meta; reject duplicates and
     non-UTF-8/absolute/`..` entry names;
   - per-entry uncompressed caps (`ZipInfo.file_size`): candidate.diff ≤ 10 MB, meta ≤ 64 KB
     (diffs are text; meta is tiny) — guards zip bombs before `read()`;
   - semantic binds: `meta["attempt_base_oid"] == inputs.attempt_base_oid`,
     `meta["execution_id"] == inputs.run_id`, `manifest_digest == sha256(diff bytes)`,
     `schema_version == 2`; any mismatch → `CandidateArchiveError` (fail closed), keeping the
     existing rule that `attempt_base_oid`/exit classification come from the trusted caller,
     never from the artifact alone (see `CandidateBundle` docstring).
5. **Keep `if: always()` on both the emit and upload steps** so cancelled/superseded attempts
   still publish their artifact (and their usage receipts) — this is also the R23
   "cancelled attempts still cost money" capture point; the concurrency group
   `forge-${{ inputs.run_id }}` with `cancel-in-progress: true` makes superseded attempts
   routine, not exceptional.

### R23 — usage receipt identity + idempotent ingestion

1. **Identity**: `receipt_id = sha256(f"{execution_id}|{attempt_id}|{driver}|{seq}|{started_at}")`
   computed **at emit time in the lane** (deterministic across re-downloads of the same
   artifact; a repair re-dispatch changes `attempt_id`, so it is a legitimately distinct
   receipt). Persist receipts with a UNIQUE index on
   `(execution_id, attempt_id, receipt_id)` and ingest with
   `INSERT ... ON CONFLICT DO NOTHING`; re-downloading the same artifact (the exact
   double-count scenario in R23) replays identical receipt ids and is a no-op, while a new
   attempt costs again. Store `github_run_id` + `github_run_attempt` alongside for
   cross-referencing the REST artifact listing.
2. **Normalization at ingestion** per the table above: derive canonical
   `input_tokens/cache_read_tokens/cache_write_tokens/output_tokens/reasoning_tokens`,
   keep `usage.raw` verbatim, keep `completeness ∈ {exact, aggregate, unknown}` with the
   current honesty rule (stream sums ⇒ `aggregate`, anything unparsable ⇒ `unknown`,
   never `0`).
3. **Failed/cancelled attempts**: the receipt travels inside the meta artifact emitted under
   `if: always()`, so a lane that burned tokens and then failed classification still
   reports spend; if the runner is killed mid-step (hard cancel), the receipt is absent —
   record the attempt with `completeness: "unknown"` and a cost-unknown marker rather than
   inventing zero (mirrors the GitLab-side "canceled job read transiently as failed" lesson
   already encoded in `_classify_failure`).
4. **Billing multipliers** live next to normalization (not in the lane): Anthropic cache
   read 0.1x, 5m write 1.25x, 1h write 2x
   ([caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching));
   OpenAI/GLM cache-read-at-serve pricing means `cached_tokens` is the only cache term they
   expose — bill it at the provider's cached rate, and leave `cache_write_tokens` unknown.

---

## Open questions

1. **Action major pinning**: the upload/download-artifact READMEs on `main` now document
   majors beyond v4 (an `archive: false` input; non-zipped artifacts changelogged
   2026-02-26). Forge pins `@v4`; dependabot config exists — decide whether R16's fix pins
   `@v4.x` exactly or adopts the newer major, and re-run the canary either way.
2. **Per-artifact byte cap**: no documented limit found (only storage quota, 500
   artifacts/job, 90-day retention). If a cap exists it is undocumented; our compressed-cap
   check is policy, not GitHub enforcement.
3. **Hard-cancel reliability of `if: always()`**: GitHub's hard cancel can kill the runner
   mid-step, so a receipt may be missing for some cancelled attempts; canary should measure
   how often (see recipe: dispatch + cancel + inspect artifacts).
4. **z.ai Anthropic-compat usage fidelity**: does `api.z.ai/api/anthropic` return the full
   `cache_creation.ephemeral_5m/1h` breakdown and `server_tool_use`, or a trimmed subset?
   Needs one live curl against the devpack endpoint before finalizing the billing table.
5. **GLM `completion_tokens` vs reasoning**: OpenRouter reports GLM-4.6 completion counts
   "may include" reasoning; z.ai's own reference does not itemize it. Confirm on live
   receipts (compare `reasoning_content` length in chars vs completion_tokens deltas)
   before treating `reasoning_tokens` as anything but unknown.
6. **Zip64 / very large artifacts**: unverified whether the v4 blob path is zip64-complete
   for >4 GB archives; forge's caps (10 MB diff) make this moot today, but note it before
   any future "lane also uploads logs" feature.
