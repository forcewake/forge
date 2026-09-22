# Script-rendering architecture — how the lane driver scripts should be built (2026-09-22)

> Research pass on the design smell flagged in `src/forge/harness_entry.py`:
> CI driver scripts are rendered as giant Python string-concatenation blobs
> (7 render arms, imperative shell assembled inline, version pins
> interpolated), while `ci/templates/*.yml` carry parallel hand-written
> script blocks — including the `<PINNED_REF>` placeholder class that
> LIVE-broke when a raw copy reached a runner. This file maps the industry
> architecture for this problem, quantifies forge's current state, and
> recommends a concrete migration.
>
> Confidence marks: **[documented]** authoritative source (linked) ·
> **[observed]** demonstrated live/measured in this repo · **[inference]**
> this pass's synthesis for forge.

## 1. The problem, precisely [observed from repo]

### 1.1 The Python render side

`render_driver_script` (`src/forge/harness_entry.py:637-1073`) is a 437-line
function with **7 per-driver arms** built from inline string concatenation:

| arm | LOC | shape |
|---|---|---|
| claude-code | 73 | env exports + MCP heredoc + retry preamble + flag invocation + tee tail |
| grok-build | 67 | retry preamble + platform-binary dance + credential blob + MCP heredoc + flag invocation |
| opencode | 34 | retry preamble + `OPENCODE_CONFIG_CONTENT` export + invocation |
| copilot | 41 | retry preamble + MCP heredoc + 14 `--allow-tool` flags |
| claude-sdk-lane | 28 | retry preamble + sdk extra + handover to `lane_driver` |
| codex-sdk-lane | 31 | retry preamble + credential blob + model export + handover |
| opencode-sdk-lane | 50 | retry preamble + permission-config export + model split + handover |
| **total** | **324** | 19% of the 1693-line module |

Duplicated fragments across arms (all counted in the file, 2026-09-22):

- the npm retry loop (`for attempt in 1 2 3; do npm install … && break …`) —
  **7 copies**, only package name/pin/CLI name varying;
- the `<cli> --version` pin-drift echo (R15) — **7 copies**;
- the `| tee -a {events} | $FORGE_FILTER_PIPE` audit tail — **4 copies**;
- the credential-blob block (`mkdir -p` / `if [ -n "$VAR" ]` / `printf` /
  `chmod 600`) — 2 near-identical copies (grok, codex);
- the MCP config heredoc (`cat > <path> <<'FORGE_MCP_EOF'`) — 3 copies;
- the quality-gate tool policy (make/uv/set/ruff/mypy/pytest + awk/sed/
  sort/cut/tr/find + venv paths) — maintained in **four dialects** in Python
  alone: the `_CLAUDE_TOOL_RULES` tuple (claude comma-list), grok
  `--allow 'Bash(x:*)'` literals, copilot `--allow-tool 'shell(x:*)'`
  literals, and opencode's JSON wildcard (`"*": "allow"`).

What actually **varies** per arm: npm package + pin, credential env/path,
MCP config path + renderer, tool-grant dialect, env exports, model routing
(flag vs env vs provider-split), and the invocation tail. Everything else is
the same skeleton — exactly the stable/variable split the precedents in §5
draw.

Two historical bug classes live in these blobs and shaped their comments:
**A09** (adjacent Python string literals silently gluing into one unmatched
permission rule — LIVE-found) and the allowlist whack-a-mole that forced
`bypassPermissions`. A09 in particular is a *string-concatenation* bug: it
cannot happen in a real `.sh` file, and it would be caught mechanically by
`bash -n`/shellcheck (§4.2).

### 1.2 The YAML template side

`ci/templates/` carries 9 lane templates (7 GitLab ~154-229 LOC each,
GitHub 332, AzDO 314) plus `harness-log-filter.mjs` (441). The GitLab
scripted templates hand-write the **same** driver invocations the Python
arms render — a parallel implementation that has already drifted
[observed]:

- `claude-code.gitlab-ci.yml:57` installs `@${FORGE_CLAUDE_VERSION:-latest}`
  while `DEFAULT_DRIVER_VERSIONS["claude-code"] = "2.1.276"` — the GitLab
  lane rides the moving dist-tag the R15 pin exists to stop;
- the GitLab claude lane uses `--permission-mode acceptEdits` + a 13-rule
  allowlist; the Actions render uses `bypassPermissions` + a 42-rule tuple
  (documented in the harness_entry docstring as deliberate, but the
  allowlist itself is a divergent copy);
- `opencode-sdk-lane.gitlab-ci.yml:106` installs opencode via the curl
  install script; harness_entry installs `opencode-ai` via npm — different
  code paths for the same pin;
- all four scripted GitLab templates curl `harness-log-filter.mjs` from
  `raw.githubusercontent.com/forcewake/forge/main/...` — **unpinned** —
  while the Actions lane fetches it from `FORGE_PINNED_REF` (default
  `main`). The filter can drift against the installed forge in both
  lanes today.

And the placeholder class: `forge-harness.github.yml:174` and
`forge-lane.azure-pipelines.yml:171` both carry
`pip install "forge @ git+https://github.com/forcewake/forge@<PINNED_REF>"`.
The onboarding docs (`docs/harnesses/onboarding.md:253`,
`docs/getting-started/github.md:84`, `docs/getting-started/azure-devops.md:103`)
instruct a human to hand-replace it. When a raw copy reaches a runner
(LIVE), pip attempts the literal `<PINNED_REF>` ref and the lane dies in
bootstrap — the exact "no placeholders in raw files" failure §4.3
describes. The GitLab sdk-lane templates already show the correct pattern
in the same repo: `"@anthropic-ai/claude-code@${FORGE_CLAUDE_VERSION:-2.1.273}"`
— a real working default plus a CI-variable override, no substitution step
a human can skip.

### 1.3 What the tests pin (the drift-catching value to preserve)

`tests/test_harness_entry.py` (1891 lines, 97 tests) asserts against the
rendered scripts by exact substring/structure:

- `TestDriverVersionPins` — for every driver, the literal
  `npm install -g --no-fund --no-audit {package}@{DEFAULT_DRIVER_VERSIONS[driver]}`
  is present, and `<cli> --version` is echoed (pin visibility);
- `TestQualityGateAllowlist` — per dialect: `Bash({gate}:*)` /
  `--allow 'Bash({gate}:*)'` / `--allow-tool 'shell({gate}:*)'` for the gate
  set, plus the mechanical commit/push deny;
- `TestPermissionListsAreTokenized` (A09) — the `--allowedTools` token list
  equals `list(_CLAUDE_TOOL_RULES)` exactly, and no glued signature
  (`*)Bash(`, `*)'--allow`, …) appears in any rendered script;
- `TestGrokLaneCredential` — exact `printf "%s" "$FORGE_GROK_AUTH" > …`
  lines present, and **forbidden** credential names absent from every other
  driver's script;
- `TestRenderSdkLanes` — lane-runner handover present, `tee -a` /
  `$FORGE_FILTER_PIPE` / prompt pointer absent.

These are semantic contracts (pins visible, gates allowed, deny intact,
credentials isolated, lists tokenized), expressed as substring greps because
that is all string blobs afford. Any refactor must keep asserting the same
contracts — ideally more mechanically (parse the script, don't grep it;
§4.2).

## 2. Script-as-package-resource [documented]

**Ship the scripts inside the wheel as package data.**

- Put non-Python files **inside the package directory**
  (`src/forge/...`), not in a sibling folder: package-internal files are
  the reliable route for wheels; root-adjacent data needs extra build
  config and breaks installed-path assumptions
  ([pyproject data-files guide](https://umatechnology.org/how-to-include-data-files-in-a-python-package-using-pyproject-toml),
  [importlib.resources overview](https://academify.com.br/en/python-importlib-resources)).
- Read them with `importlib.resources.files()` — never `open()` relative
  paths or `__file__` joins, which work in editable installs and fail in
  real wheels ([importlib.resources docs](https://docs.python.org/3/library/importlib.resources.html);
  use `as_file()` only when a real path is required).
- Backend config: hatchling takes explicit `include` patterns per target
  (wheel/sdist); setuptools uses `[tool.setuptools.package-data]`
  ([same guide](https://umatechnology.org/how-to-include-data-files-in-a-python-package-using-pyproject-toml)).
  Forge already builds with
  `[tool.hatch.build.targets.wheel] packages = ["src/forge"]` — files under
  `src/forge/` ride the wheel by default; an explicit include documents
  intent.
- Verify the wheel actually contains them (`unzip -l dist/*.whl`) and wire
  that into CI — the classic failure is "works locally (editable), missing
  after install" [documented,
  guide](https://umatechnology.org/how-to-include-data-files-in-a-python-package-using-pyproject-toml).
  forge has a second, sharper version of this failure today: the lane
  fetches `harness-log-filter.mjs` over the network precisely because it
  is NOT in the wheel [observed].

**Rendering engine choice** (for whatever substitution remains after §4.1):

| engine | verdict |
|---|---|
| `envsubst` | No conditionals, no loops, no defaults, no validation — and it would substitute *runtime* shell vars (`$PATH`, `$attempt`) it was never meant to touch unless carefully allow-listed ([envsubst alternatives](https://karandeepsingh.ca/posts/alternatives-to-envsubst), [envsubst→Go guide](https://karandeepsingh.ca/posts/envsubst-jinja2-templating-guide)). Wrong tool for executable scripts. |
| `string.Template` (stdlib) | Safe single-level substitution; `$`-delimiters collide with shell's own `$`, so a custom delimiter is needed; conditionals/loops live in the Python caller. Enough once conditionals are pushed into bash (§4.1). |
| Jinja2 | Full conditionals/loops/filters/defaults — the engine Ansible/Salt standardized on ([alternatives-to-envsubst](https://karandeepsingh.ca/posts/alternatives-to-envsubst)). Correct tool *if* render-time branching stays. Risk for forge: render-time `{% if %}` hides shell structure from shellcheck and re-creates the arms in a second language. |

**Production precedent for executable templates shipped from a package**:
pre-commit installs a tiny static stub into `.git/hooks/` whose entire body
delegates to the pinned installed binary
([behind-the-scenes](https://stefaniemolin.com/articles/devx/pre-commit/behind-the-scenes)),
and hook repos are consumed pinned by `rev`
([pre-commit.com](https://pre-commit.com/)). The deployed artifact is a
**thin dispatcher; the intelligence ships versioned inside the package**.
Forge's `harness_entry` is already that dispatcher for Actions/AzDO — the
open question is only where its script bodies live.

## 3. CI-native composition [documented]

**GitHub:**

- A **composite action** (`action.yml` with `runs.using: "composite"`)
  bundles steps that run *inside the caller's job* on the caller's runner;
  every `run:` step needs an explicit `shell:`; referenced cross-repo as
  `OWNER/repo@ref` — same SHA-pinning posture forge already mandates
  ([GitHub Docs: creating a composite action](https://docs.github.com/en/actions/sharing-automations/creating-actions/creating-a-composite-action)).
  Key limitation: **the `secrets` context is not available inside
  composite action YAML** — secrets must be passed as inputs (and then via
  `env:` on the step) or the unit must be a reusable workflow instead
  ([Kaschimer: composites vs reusable workflows](https://steve-kaschimer.github.io/posts/2026-03-13-github-actions-reusable-workflows-vs-composite-actions)).
- A **reusable workflow** (`on: workflow_call`) is the job-level unit: own
  runner, own secrets boundary, `secrets: inherit` or explicit — the right
  shape when the steps must run on a different machine/context than the
  caller ([comparison](https://nerdleveltech.com/github-actions-reusable-workflow-vs-composite-action),
  [GitHub's own table](https://docs.github.com/en/actions/sharing-automations/reusing-workflows)).
  forge's dispatched `forge-harness.github.yml` already occupies this
  ecological niche.
- **Script injection**: `${{ }}` is *textual substitution into the script
  before the shell runs*; anything attacker-influenceable (issue/PR titles,
  bodies, branch names, and `inputs.*` of reusable/composite units) must go
  through an intermediate `env:` variable, and actions should be pinned to
  full SHAs ([GitHub secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use);
  the rule enforced mechanically by actionlint, [Copilot-stack writeup](https://thecopilotstack.com/github-copilot/devops/github-actions)).
  forge's template interpolates `${{ inputs.attempt_base_oid }}` directly
  into a `run:` block (emit step) — control-plane-trusted today, but the
  documented hard pattern is env-intermediation, which the rest of the file
  already uses [observed].

**GitLab:**

- **CI/CD components** are versioned reusable units (`@<tag-or-SHA>`),
  with `spec:inputs` validated *at pipeline creation* (a missing required
  input fails before any runner works), published to a Catalog, living in
  a `templates/` directory ([GitLab Docs: CI/CD components](https://docs.gitlab.com/ee/ci/components/)).
- Classic `include:` variants ranked by integrity: `include:component` /
  `include:project` (pinnable by ref) beat `include:remote` — "highest
  integrity risk; prefer pinned or internally mirrored sources"
  ([product-security.expert on reusable includes](https://product-security.expert/07-ci-cd-and-software-supply-chain/reusable-gitlab-includes-and-components)).
  forge's documented onboarding include rides `…/forge/main/ci/templates/…`
  — an unpinned moving branch [observed,
  claude-code.gitlab-ci.yml:7].
- The migration story is directly on point: shared logic moves from
  copy-pasted or floating-`main` remote includes to a versioned component
  whose contract (`spec:inputs`) GitLab validates
  ([from legacy includes to the component catalog](https://gitdash.dev/blog/gitlab-ci-component-catalog-migration)).

**Where should the script live — target repo or dispatcher?** The
setup-* / starter-workflow pattern: the target repo carries a *thin*,
human-reviewable unit; the heavy logic lives in the versioned action or
package it invokes. Forge's thin-unit boundary already exists — it is the
`pip install forge@<ref>` + `python -m forge.harness_entry` step pair.

## 4. Template hygiene [documented + observed]

### 4.1 Push conditionals into the runtime, not the renderer

The strongest convergent finding of this pass: mature systems make the
shipped artifact *static and valid as-is*, and deliver the variable parts
as **environment** the script reads at runtime with real defaults:

- Buildkite injects plugin configuration as `BUILDKITE_PLUGIN_<NAME>_*`
  env vars — never string interpolation — and the hooks are plain shell
  ([Buildkite agent plugins](https://mintlify.wiki/buildkite/agent/configuration/plugins));
- GitHub's hardening rule is the same shape: untrusted (and ideally all)
  values enter scripts via `env:`, not `${{ }}`
  ([secure-use reference](https://docs.github.com/en/actions/reference/security/secure-use));
- NativeLink config files use shell-default expansion
  `${VAR:-real-default}` so one file works everywhere, overridden by env
  ([NativeLink config docs](https://docs.nativelink.com/configuration/config-file));
- forge itself already does this in the good arms: `MAX_THINKING_TOKENS`
  is set from `"${FORGE_MAX_THINKING_TOKENS:-8000}"` and the sdk-lane
  GitLab templates pin via `${FORGE_CLAUDE_VERSION:-2.1.273}` [observed].

When the script branches in bash (`if [ -n "$MODEL" ]`, `${VAR:-default}`),
the renderer's job degenerates to trivial substitution — which
`string.Template` (or plain `.replace` on a small token table) handles
without an engine. Jinja2 only becomes necessary if render-time branching
is kept, which §7 argues against: `{% if %}` decisions are invisible to
shellcheck.

### 4.2 Lint the rendered artifact, mechanically

- GitLab's own development guide mandates a `shellcheck` CI job (plus
  `shfmt -d` formatting check) for every project's shell scripts
  ([shell scripting guide](https://docs.gitlab.com/development/shell_scripting_guide/));
- shellcheck exits nonzero on findings, so it gates CI with no wrapper;
  `bash -n` catches parse errors for free
  ([check/lint/format bash](https://shell-tips.com/bash/syntax-check-test-lint-format));
- actionlint statically flags untrusted-context interpolation in workflow
  YAML ([Copilot-stack writeup](https://thecopilotstack.com/github-copilot/devops/github-actions)).

forge currently has **no shellcheck/shfmt gate anywhere** — the driver
scripts exist only inside Python strings, so no shell tool can see them
[observed]. The A09 glued-rule class and every quoting regression in these
blobs is exactly what `bash -n` + shellcheck over the *rendered* output
(all 7 drivers × MCP on/off) would catch deterministically, replacing
substring-grep approximations of the same guarantees.

### 4.3 No placeholders in raw files

The `<PINNED_REF>` bug class has a name in the research: config files must
be *valid without substitution* — real working defaults, environment
override at runtime ([NativeLink](https://docs.nativelink.com/configuration/config-file);
[twelve-factor structure-vs-secrets split](https://rebash.in/python/configuration-management-and-secrets)).
A `<TOKEN>` that a human must replace is a build-time substitution step
whose failure mode is "the runner sees the literal token" — which is what
LIVE-happened. The fix is not better substitution tooling; it is removing
the substitution step: ship `forge @ git+…@<released-tag>` as the default
and let a repo variable (or the dispatch input) override it.

### 4.4 YAML: handwritten-but-thin beats generated

Generating YAML programmatically (PyYAML `safe_dump`, js-yaml) is the
right call when the YAML itself is machine-consumed
([templating YAML with real code](https://learnkube.com/templating-yaml-with-code)).
But forge's templates are **human-applied at onboarding** and read by the
repo owner; a codegen artifact is unreviewable there [inference]. The
hygiene win is the opposite direction: keep the YAML skeleton
handwritten and *thin* (rules, variables, artifacts, one install+dispatch
step), and move every script body out of YAML into the package (§2) or a
versioned CI-native unit (§3). YAML anchors/`extends` help within one
file but cannot span the 9 templates or the Python arms.

## 5. Precedent hunt: control planes rendering runner scripts [documented]

1. **GitLab Runner** implements per-shell *script generators*: the runner
   builds a script skeleton (clone, cache restore, build commands, cache
   update, artifacts) and splices the user's `script:` sections in as
   data; bash is piped to `cat generated-bash-script | /bin/bash`, pwsh
   is saved to a file and invoked ([shells supported](https://docs.gitlab.com/runner/shells/)).
   The engineering vision names the design: an *abstract shell* converting
   a job into target scripts, with the job split into fixed stages
   (get_sources, restore_cache, …, user_script, …)
   ([runner technical problems](https://handbook.gitlab.com/handbook/engineering/architecture/design-documents/runner_technical_vision/problems)).
   **Split**: the skeleton is versioned with the agent binary; only the
   user's commands vary, spliced at fixed points.
2. **Buildkite agent hooks/plugins**: the agent runs plain shell *hooks*
   at lifecycle points; *plugins* are git repositories pinned `#v1.0.0`
   that the agent clones at job runtime and caches; plugin config arrives
   as env vars; `--allowed-plugins` regex-gates what an agent will load
   ([plugins](https://mintlify.wiki/buildkite/agent/configuration/plugins),
   [hooks](https://mintlify.wiki/buildkite/agent/configuration/hooks),
   [official hooks doc](https://buildkite.com/docs/agent/v3/hooks)).
   **Split**: what varies = env vars + a pinned ref; what's stable = the
   hook scripts inside the versioned plugin. This is the closest analogue
   to forge's lane (and the pin/grant/env contract shape matches).
3. **pre-commit**: thin static hook stub delegating to the pinned
   installed package; hooks pinned by `rev`; environments provisioned per
   hook ([pre-commit.com](https://pre-commit.com/),
   [behind the scenes](https://stefaniemolin.com/articles/devx/pre-commit/behind-the-scenes)).
   **Split**: dispatcher ≈ 10 lines; everything else ships in the package.
4. **Dagger / Earthly**: pipelines as real code (SDK functions /
   Earthfile) — same definition runs locally and in CI, testable and
   reviewable like any code ([Dagger vs Earthly vs GHA](https://devopsboys.com/blog/dagger-vs-earthly-vs-github-actions-ci-comparison-2026),
   [portability guide](https://bigiron.cc/guides/earthly-vs-dagger-vs-just-make-build-pipeline-portability)).
   **Lesson applied to forge**: the render function and its output should
   be first-class testable artifacts — not YAML, and not opaque strings.

Common denominator: **the stable skeleton ships versioned with the
tool; the variable parts (pins, env, grants) cross the boundary as data
(env vars or validated inputs), never as textual surgery on the script.**

## 6. Trade-off matrix

Criteria weighted for forge's reality: lane runs on pinned forge (never
moving refs), proposal-only security posture, tests must keep catching
drift, onboarding is human-applied, three CI platforms supported.

| # | Option | Kills <PINNED_REF> class | Kills A09 glue class | shellcheck-able | GitLab↔Actions drift | Test value kept | Effort / risk |
|---|---|---|---|---|---|---|---|
| 1 | **Status quo** (string arms + hand-written YAML) | no | no (guard + tests only) | no | no (drifting today) | yes (substring) | — / bugs recur |
| 2 | **Script files as package data + importlib.resources + thin renderer (env-contract, conditionals in bash)** | yes (with §4.3 defaults) | yes (real `.sh` + `bash -n`) | **yes** | partially (Python arms unify; GitLab YAML still parallel until step 5) | yes (assert contracts on real files; keep exact-list tests) | medium / low with golden-file bridge |
| 3 | Option 2 + **Jinja2** engine | yes | yes | partially (`{% if %}` output still lintable, logic less visible) | partially | yes | medium+ / medium (new dep in the lane install; second language for logic) |
| 4 | **CI-native units** (composite action / GitLab component) + thin dispatcher | yes (ref-pinned units) | yes (steps are scripts) | partially (per-step bodies) | **yes across platforms' native mechanism** | partially (contract tests move to the unit repo) | high / medium (secrets-context limits on composites; catalog setup on GitLab CE; two release trains) |
| 5 | **Full YAML codegen** from the control plane | yes | n/a | n/a | yes | weak (artifacts unreviewable) | high / high (owners can't review applied files; diff noise every bump) |
| 6 | **Hybrid (recommended)**: option 2 for driver scripts + §4.3 defaults in templates + keep YAML thin + converge GitLab scripted lanes onto the install-forge→harness_entry dispatch (what the sdk-lane templates and Actions/AzDO already do) | **yes** | **yes** | **yes** | **yes** (single render source; YAML keeps only rules/artifacts) | **yes, stronger** (semantic + mechanical gates) | staged / low→medium (each phase byte-identical or deliberately gated) |

## 7. Recommendation

**Option 6 — the thin-dispatcher architecture forge already half-has.**
The Actions and AzDO lanes already install pinned forge and let
`harness_entry` render the driver; the sdk-lane GitLab templates already
provision-and-hand-over. Finish that convergence:

1. Driver scripts become **real `.sh` files shipped in the wheel** under
   `src/forge/harnesses/scripts/` (read via `importlib.resources`), with a
   per-driver file plus shared fragments (npm preamble, credential blob,
   MCP provision, tee tail) `source`d or concatenated by the renderer.
2. **Conditionals live in bash**: `${FORGE_DRIVER_VERSIONS:-}`-style
   runtime defaults and `if [ -n "$MODEL" ]` guards; the renderer does
   token substitution only (stdlib `string.Template` with a non-`$`
   delimiter, fed from `resolve_driver_versions` — keeping the existing
   fail-closed charset check `_DRIVER_VERSION_RE` as the injection guard).
   No Jinja2: render-time logic is what shellcheck can't see.
3. **Pins stay single-source** in `DEFAULT_DRIVER_VERSIONS`; the three
   tool-grant dialects derive from ONE ordered policy table (generalize
   `_CLAUDE_TOOL_RULES` + the `TestQualityGateAllowlist._GATES` set) with
   per-dialect formatters. A new consistency test parses
   `ci/templates/*.gitlab-ci.yml` inline version defaults and asserts
   equality with `DEFAULT_DRIVER_VERSIONS` — it would fail today on
   claude-code (`latest` vs `2.1.276`) [observed], which is exactly its
   job until phase 5 lands.
4. **The `<PINNED_REF>` class disappears structurally**: templates ship
   `pip install "forge @ git+…@<released-tag>"` as the real default with a
   `FORGE_REF`-style env/variable override; and `harness-log-filter.mjs`
   moves into the wheel (same package data), deleting the
   raw.githubusercontent fetch — the filter then rides the same pin as the
   code it filters, closing the main-vs-pin drift in both lane families.
5. **Mechanical gates**: a CI job renders every driver × MCP on/off and
   runs `bash -n` + `shellcheck` (+ `shfmt -d`) over the output — the
   deterministic replacement for substring approximations; existing test
   classes keep their semantic assertions (exact token lists, credential
   isolation, mechanical deny) against the rendered bytes.

### Migration sketch, ordered by risk

**Phase 0 — golden bridge (no behavior change).** Commit rendered fixtures:
all 7 drivers × (no MCP, one HTTP MCP server) →
`tests/fixtures/rendered/<driver>[-mcp].sh`. One test asserts
`render_driver_script` output equals the fixture byte-for-byte. Everything
later is now provably byte-identical or deliberately reviewed.

**Phase 1 — extract to package data.** Create
`src/forge/harnesses/scripts/{common/,drivers/}`; move the log filter to
`src/forge/harnesses/scripts/harness-log-filter.mjs`. New
`forge/harnesses/script_render.py` loads templates via
`importlib.resources`, applies the token table, returns the same bytes
(golden test enforces). `harness_entry.render_driver_script` becomes a
thin delegate; `main()` fetches the filter from package data first,
network fallback second (transitional). Add the wheel-content CI check
(build wheel, `unzip -l | grep scripts/`, install into a clean venv and
render once).

**Phase 2 — mechanical gates + test conversion.** CI job: render fixtures
→ `bash -n` + `shellcheck --severity=warning` (+ `shfmt -d` advisory);
fix findings (expected: quoting in the tee tails, the grok `test -d`
line). Convert `TestPermissionListsAreTokenized`/`TestQualityGateAllowlist`
to assert on parsed shell structures where cheap; keep exact-list equality
assertions as the drift contract. `actionlint` over `ci/templates/*.yml`
and the GitHub template.

**Phase 3 — de-placeholderize the YAML.** GitHub + AzDO templates:
`pip install "forge @ git+…@v0.25.0"` (the current released tag) with the
documented env/variable override; delete the hand-replacement step from
the three onboarding docs; add a template consistency test asserting no
`<[A-Z_]+>` token exists in any `ci/templates` file. Env-intermediate the
emit step's `${{ inputs.attempt_base_oid }}`.

**Phase 4 — pin-consistency fence.** The GitLab-template ↔
`DEFAULT_DRIVER_VERSIONS` equality test (point 3 above); pin the
documented GitLab `include: remote` to a tag (or move to
`include: component` if the target GitLab instances have a catalog).

**Phase 5 — converge the scripted GitLab lanes (bigger, separate
approval).** The four scripted `*.gitlab-ci.yml` templates shrink to the
sdk-lane shape: rules/variables/artifacts skeleton + install pinned forge
+ `python -m forge.harness_entry` (their `node:22-bookworm` images need a
python addition; the sdk-lane templates already solved this). The
hand-written invocations — and their drift — are deleted.

### File layout after phases 1-3

```
src/forge/harnesses/
  scripts/
    common/npm-pin.sh          # retry loop; @@PACKAGE@@/@@PIN@@/@@CLI@@ tokens
    common/credential-blob.sh   # @@ENV_VAR@@/@@DEST@@ tokens
    common/mcp-heredoc.sh       # @@DEST@@/@@JSON@@ tokens
    common/tee-tail.sh          # @@EVENTS@@ token
    drivers/claude-code.sh      # env-contract: FORGE_*, MODEL, MCP via render
    drivers/grok-build.sh
    drivers/opencode.sh
    drivers/copilot.sh
    drivers/{claude,codex,opencode}-sdk-lane.sh
    harness-log-filter.mjs      # rides the wheel; same pin as the code
  script_render.py              # token table + string.Template + resource loading
  harness_entry.py              # thin: resolve → validate charset → render → bash -c
ci/templates/*.yml              # thin skeletons; real-default pins; no <TOKEN>s
tests/fixtures/rendered/*.sh    # golden files (phase 0, retained as contract)
```

## 8. What NOT to do

- **Do not adopt Jinja2 as the load-bearing fix.** Render-time conditionals
  re-create the seven arms in a second language, hide control flow from
  shellcheck, and add a lane-install dependency — the research pattern
  (Buildkite, GitHub hardening, NativeLink) is runtime env + near-static
  scripts, not smarter render-time substitution. [inference from §2/§4.1]
- **Do not generate whole workflow YAML from Python.** Codegen output is
  unreviewable by the repo owners who must apply it at onboarding, and
  every pin bump rewrites the file (approval churn). Keep YAML
  handwritten-but-thin. [§4.4]
- **Do not leave raw `.sh`/`.mjs` files only in `ci/templates/` and fetch
  them over the network at lane time.** That is the current log-filter
  posture, and it is the live instance of pin drift (filter rides `main`
  while forge rides a ref). Executable lane assets ride the wheel. [§1.2]
- **Do not replace the exact-list tests with looser greps.**
  `tokens == list(_CLAUDE_TOOL_RULES)`, the forbidden-credential matrix and
  the glued-signature scan ARE the drift-catching value; a refactor that
  weakens them to "contains the word pytest" has lost the A09 lesson.
  Mechanical gates are added on top, not instead. [§1.3]
- **Do not interpolate values into `run:` steps where `env:` works** —
  including trusted dispatch inputs; the documented posture is
  env-intermediation everywhere, and actionlint will eventually flag the
  rest. [§3]
- **Do not make the pin set configurable from the target repo without the
  fail-closed charset check.** The pin lands in a shell command;
  `_DRIVER_VERSION_RE` (and its ValueError path) moves with the renderer
  wherever the tokens are substituted. [§1.1, R15]

## 9. Source index

PyPA / packaging: [data files in pyproject.toml](https://umatechnology.org/how-to-include-data-files-in-a-python-package-using-pyproject-toml) ·
[importlib.resources](https://docs.python.org/3/library/importlib.resources.html) ·
[resource-loading pitfalls](https://runebook.dev/en/docs/python/library/importlib.resources) ·
[pre-commit](https://pre-commit.com/) ·
[pre-commit internals](https://stefaniemolin.com/articles/devx/pre-commit/behind-the-scenes)

Templating engines: [envsubst alternatives](https://karandeepsingh.ca/posts/alternatives-to-envsubst) ·
[envsubst → Go/templating guide](https://karandeepsingh.ca/posts/envsubst-jinja2-templating-guide)

CI-native composition: [composite actions (GitHub Docs)](https://docs.github.com/en/actions/sharing-automations/creating-actions/creating-a-composite-action) ·
[reusable vs composite](https://nerdleveltech.com/github-actions-reusable-workflow-vs-composite-action) ·
[composites' secrets gotcha](https://steve-kaschimer.github.io/posts/2026-03-13-github-actions-reusable-workflows-vs-composite-actions) ·
[GitLab CI/CD components](https://docs.gitlab.com/ee/ci/components/) ·
[reusable GitLab includes ranked](https://product-security.expert/07-ci-cd-and-software-supply-chain/reusable-gitlab-includes-and-components) ·
[component-catalog migration](https://gitdash.dev/blog/gitlab-ci-component-catalog-migration)

Security: [GitHub secure-use reference (script injection)](https://docs.github.com/en/actions/reference/security/secure-use) ·
[actionlint catching untrusted interpolation](https://thecopilotstack.com/github-copilot/devops/github-actions) ·
[composite-action shell injection case](https://orbisappsec.com/blog/how-run-shell-injection-happens-in-github-actions-and-how-to-fix)

Hygiene: [GitLab shell scripting guide (shellcheck/shfmt CI)](https://docs.gitlab.com/development/shell_scripting_guide/) ·
[bash check/lint/format](https://shell-tips.com/bash/syntax-check-test-lint-format) ·
[templating YAML with real code](https://learnkube.com/templating-yaml-with-code) ·
[NativeLink ${VAR:-default} config](https://docs.nativelink.com/configuration/config-file) ·
[12-factor structure vs secrets](https://rebash.in/python/configuration-management-and-secrets)

Precedents: [GitLab Runner shells](https://docs.gitlab.com/runner/shells/) ·
[runner technical vision](https://handbook.gitlab.com/handbook/engineering/architecture/design-documents/runner_technical_vision/problems) ·
[Buildkite plugins](https://mintlify.wiki/buildkite/agent/configuration/plugins) ·
[Buildkite hooks](https://mintlify.wiki/buildkite/agent/configuration/hooks) ·
[Dagger vs Earthly vs GHA](https://devopsboys.com/blog/dagger-vs-earthly-vs-github-actions-ci-comparison-2026) ·
[build-pipeline portability](https://bigiron.cc/guides/earthly-vs-dagger-vs-just-make-build-pipeline-portability)
