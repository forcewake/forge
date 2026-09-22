# Patch application: git semantics, library survey, and a design for CandidateBundle materialization (R08/R09)

Research input for the fix of review findings **R08** ("an UPDATE with `new_content` and
empty hunks silently materializes to the ORIGINAL file") and **R09** (hand-rolled
unified-diff parser/applier wrong for zero-context insertions, CRLF, missing hunk
counts, silently dropped mode-only changes, final-newline handling, binary/rename
policy). Findings reference `src/forge/runs/candidate.py`
(`parse_unified_diff` / `CandidateBundle.materialize` / `apply_unified_hunks`) and
ADR-0016 / ADR-0001.

All "verified" items marked **[verified 2026-09-17]** were reproduced locally against
git 2.x (`git apply`), forge (`src/forge/runs/candidate.py`), patch-ng 1.19.1 and
whatthepatch 1.0.7; the exact commands are in the appendix.

---

## Ground truths (with source URLs)

### G1. What `@@ -l,0 +c,k @@` means: the zero-context insertion point

POSIX `diff` (IEEE Std 1003.1) defines the range fields of the unified hunk header:

> "Each range field shall be of the form `%1d` (beginning line number), or `%1d,1` if
> the range contains exactly one line, and `%1d,%1d` (beginning line number, number of
> lines) otherwise." and "**If a range is empty, its beginning line number shall be the
> number of the line just before the range, or 0 if the empty range starts the file.**"

Source: <https://pubs.opengroup.org/onlinepubs/9799919799/utilities/diff.html>

So for `@@ -3,0 +4,1 @@` the old range is *empty* and its "beginning line" is 3 — the
insertion happens **after line 3** of the old file (0-based slice index 3 in a
line list, i.e. `lines[:3] + ["INSERTED"] + lines[3:]`). The forge rule
`target = max(hunk.old_start - 1, 0)` unconditionally computes index 2 → inserts
after line 2.

**[verified 2026-09-17]** With base `line1..line4`, forge materializes
`@@ -3,0 +4,1 @@ +INSERTED` to `line1, line2, INSERTED, line3, line4` (silently
wrong). `git apply --unidiff-zero` yields the correct
`line1, line2, line3, INSERTED, line4`. patch-ng 1.19.1 (latest) has the **same bug**
(inserts after line 2, returns success — see L4); whatthepatch's pure-Python
`apply_diff` inserts at `new - 1` and is correct.

Correct rule (as in git/whatthepatch): slice index `= old_count == 0 ? old_start :
old_start - 1`.

### G2. `git apply` expects context by default; `--unidiff-zero` is a separate mode

> "By default, git apply expects that the patch being applied is a unified diff with at
> least one line of context. This provides good safety measures, but breaks down when
> applying a diff generated with `--unified=0`. To bypass these checks use
> `--unidiff-zero`." and "Note, for the reasons stated above, the usage of
> context-free patches is discouraged."

Source: <https://git-scm.com/docs/git-apply>

The implementation (apply.c) is more surprising than the doc: without
`--unidiff-zero`, a hunk with zero trailing context gets `match_end = 1` ("A hunk
without trailing lines must match at the end"), and a hunk whose `oldpos <= 1` gets
`match_beginning = 1`:

```c
match_beginning = (!frag->oldpos || (frag->oldpos == 1 && !state->unidiff_zero));
match_end = !state->unidiff_zero && !trailing;
```

Source: <https://github.com/git/git/blob/master/apply.c> (`apply_one_fragment`, around
lines 3132–3145 in master, 2026-09).

**[verified 2026-09-17]** Plain `git apply` of a `git diff -U0` hunk
`@@ -3,0 +4 @@ +INSERTED` **silently applies it at end-of-file**
(`line1,line2,line3,line4,INSERTED`, exit 0). Only `git apply --unidiff-zero` applies
it after line 3. Consequence for forge: if zero-context hunks are ever accepted, they
must be applied with the G1 positional rule, *not* delegated to bare
`git apply` without `--unidiff-zero` — bare delegation is wrong, not just
conservative.

### G3. How git matches hunks: exact bytes first, offsets allowed, fuzz only on request

From apply.c (<https://github.com/git/git/blob/master/apply.c>):

- Each fragment carries `oldpos, oldlines, newpos, newlines` plus derived
  `leading`/`trailing` context counts (`struct fragment`, ~line 245). The declared
  counts are consumed exactly while parsing the body — a mismatch is
  `error: corrupt patch at line N` (parse_fragment; classic symptom:
  <https://stackoverflow.com/questions/18142870/git-error-fatal-corrupt-patch-at-line-36>).
- Search start: `pos = frag->newpos ? (frag->newpos - 1) : 0`. `find_pos` tries the
  recorded position first, then walks **one line backward, one forward, two backward,
  two forward, …** until the preimage matches; a move is reported as
  `Hunk #N succeeded at X (offset Y lines)` under `--verbose`. So **line offsets are
  allowed by default; content never is.**
- `match_fragment` requires an exact byte comparison of the whole preimage
  (`memcmp` of the joined lines, with a per-line hash prefilter), plus boundary
  anchoring (`match_beginning` → must be at line 0; `match_end` → must end at EOF).
  A `LINE_PATCHED` flag forbids two hunks from consuming the same lines.
- The only fuzzy paths are opt-in: `--ignore-whitespace`
  (`line_by_line_fuzzy_match`) and `--whitespace=fix` (retry after correcting
  whitespace errors). `-C<n>` allows giving up context lines as a last resort
  (default `-C` is "no context is ever ignored").

Docs: <https://git-scm.com/docs/git-apply> (`--ignore-space-change`,
`--whitespace=<nowarn|warn|fix|error|error-all>`, `-C<n>`, `--recount`,
`--inaccurate-eof`).

Forge's "strict, exact position, no fuzz, no offset" policy is therefore *stricter
than git*. That is a defensible product choice (deterministic, no silent drift), but
it must then be paired with **rejecting** (not silently mis-applying) anything git
would apply with an offset, and the strictness must be stated in the rejection reason.

### G4. Atomicity

> "For atomicity, git apply by default fails the whole patch and does not touch the
> working tree when some of the hunks do not apply. This option [--reject] makes it
> apply the parts of the patch that are applicable, and leave the rejected hunks in
> corresponding \*.rej files."

Source: <https://git-scm.com/docs/git-apply>

Matches forge's "reject whole operation with reason" acceptance criterion — the
policy to copy: no partial application, no `.rej`-style partial materialization.

### G5. Final-newline (`\ No newline at end of file`)

- The marker follows the single ` `/`+`/`-` line whose terminating newline it removes;
  git's parser accepts any locale spelling beginning with `"\\ "` (comment in
  apply.c: "Depending on locale settings … we don't know what this line would exactly
  say. The only thing we do know is that it begins with `\ `"), and
  `apply_one_fragment` drops one newline (`plen--`) from that line only.
  Source: <https://github.com/git/git/blob/master/apply.c> (~lines 1735–1755 and
  3029–3049). Parsing-side harmonization commit (2025):
  <https://gitlab.com/Minion3665/git/-/commit/3a4eb5ad2e9166255d5921196470710523f24ec4>
  ("apply: revamp the parsing of incomplete lines" — fork mirror of git/git).
- A missing final newline is a first-class content property: git diff emits the marker
  on the old side, the new side, or both, and `git apply` reproduces the missing
  newline byte-exactly.
  **[verified 2026-09-17]** base `x\ny` (no final NL) → `x\ny\nz` (no final NL)
  round-trips through `git diff` / `git apply` with the marker on both sides
  (`xxd`: `78 0a 79 0a 7a`).
- Related whitespace machinery: `core.whitespace` has an `incomplete-line` rule
  (treats missing final newline as an error, **not enabled by default**), and
  `--whitespace=fix` may strip blank lines added at EOF. Forge should never enable
  fixing; it should verify bytes instead.

Design implication: the applier must track "does the file end with newline" as an
explicit bit on both sides and reject mismatches; converting the whole text through
`str.splitlines()` destroys exactly this information (see G6, R09).

### G6. CRLF / autocrlf: patches match *converted* content; CR is content, not whitespace

- Working-tree files may differ from blob bytes (core.autocrlf / .gitattributes).
  `git apply` matches the patch against the *clean-converted* target
  (`read_old_data()` runs `convert_to_git(...)`), except when the patch's old lines
  themselves contain CRLF: commit c24f3ab (2017) "apply: file committed with CRLF
  should roundtrip diff and apply" sets `crlf_in_old`, skips the CRLF→LF conversion
  (`SAFE_CRLF_KEEP_CRLF`) and sets `WS_CR_AT_EOL` so CR is no longer treated as
  whitespace. Sources:
  <https://github.com/git/git/commit/c24f3abac> and
  <https://git-scm.com/docs/git-apply> (`--ignore-space-change` semantics).
  **[verified 2026-09-17]** A CRLF file committed with CRLF, diffed with
  `core.autocrlf=false`, applies with `git apply --check` and round-trips exactly.
- CRLF can also leak into *structural* diff lines (e.g. `--- /dev/null\r`), which
  historically broke `git apply` header parsing ("bad git-diff - expected /dev/null");
  fixed by accepting `/dev/null` followed by any whitespace:
  <https://public-inbox.org/git/xmqqoau6hz1t.fsf@gitster.dls.corp.google.com/T>
- GNU `patch(1)` behaves differently: it warns "(Stripping trailing CRs from patch.)"
  and treats line-ending mismatch as a failure mode ("Hunk #1 FAILED … different line
  endings") — do not use `patch(1)` behavior as the reference:
  <https://unix.stackexchange.com/questions/239364/how-to-fix-hunk-1-failed-at-1-different-line-endings-message>

Forge bug (R09): the parser reads the diff with `str.splitlines()` (which strips a
trailing `\r` from every line **and** splits on `\r`, `\v`, `\f`, `\x1c`–`\x1e`,
`\x85`, `\u2028`, `\u2029`), while the applier splits the base with `split('\n')`
(which keeps `\r`). A perfectly valid CRLF diff is therefore rejected as
`patch_does_not_apply` **[verified 2026-09-17]**, and a hunk line containing U+2028
(e.g. inside a JS string literal) is split mid-line and corrupts the hunk
**[verified 2026-09-17]**. Fix: split the diff on `"\n"` only (or work on `bytes`),
keep CR as content.

### G7. Modes, renames, binary — the extended-header contract

`git diff`/`git apply` extended headers (parser table in apply.c ~line 1399, sources
<https://github.com/git/git/blob/master/apply.c> and
<https://git-scm.com/docs/git-apply>):

| Header | Meaning | git apply behavior |
|---|---|---|
| `old mode <m>` / `new mode <m>` | mode-only change, **no hunks** | applies chmod (a "pure mode change" has no index info; see `--build-fake-ancestor`) |
| `new file mode <m>` + `--- /dev/null` | creation | creates file, sets mode; empty file = `@@ -0,0 +0,0 @@` or no hunk |
| `deleted file mode <m>` + `+++ /dev/null` | deletion | removes file |
| `rename from`/`rename to`, `copy from`/`copy to` | rename/copy (optionally with hunks) | applies rename/copy; typechange is split by git into delete+create pairs (comment ~line 4090) |
| `GIT binary patch` + `literal N`/`delta N` (deflated) | binary content | applies (the `--binary` flag is now "a no-op"; application is always allowed) |
| `index <old>..<new> <mode>` | blob OIDs (full with `--full-index`) | `--3way` uses them for a 3-way merge fallback |

Error catalog worth mirroring in forge's rejection reasons (apply.c ~lines
4110–4180): `already exists in index/working directory`, `new mode (%o) does not match
old mode (%o)`, `affected file '%s' is beyond a symbolic link`, and the umbrella
`%s: patch does not apply`.

Forge today **silently drops** mode-only entries and **rejects** rename/binary — R09
demands the mode-only case also becomes an explicit rejection
(`mode_change_not_supported`), never a silent no-op.

### G8. The useful `git apply` gates for forge

- `git apply --check` — "Instead of applying the patch, see if the patch is applicable
  to the current working tree and/or the index file and detects errors. Turns off
  'apply'." (<https://git-scm.com/docs/git-apply>)
- `--3way` — blob-identity 3-way merge fallback; **forbidden for forge** materialization
  (it can fabricate merged content that never matched the base; forge requires exact).
- `--directory=<root>`, `-p<n>`, `--include`/`--exclude`, `--unsafe-paths` — path
  scoping for the sandboxed check.
- `--whitespace=nowarn` for deterministic stderr parsing; never `fix`.

---

## Library survey (table)

Verified against PyPI/GitHub metadata (fetched 2026-09-17) and local runs of
patch-ng 1.19.1, whatthepatch 1.0.7 and unidiff 1.x **[verified 2026-09-17]**.

| Library | Latest / activity | License | Parse | Apply | Zero-context (`-3,0`) | Counts cross-check | CRLF | No-final-NL | Binary | Modes/renames | Verdict for forge |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **unidiff** (matiasb/python-unidiff) | 1.0.4, 2026-07; very active (43 M downloads/mo) <https://github.com/matiasb/python-unidiff>, <https://pypi.org/project/unidiff/> | MIT | yes | **no** (parse/inspect only) | parses correctly (`source_start=3, source_length=0`) **[verified]** | **yes** — `UnidiffParseError: Hunk is shorter than expected` **[verified]** | requires `newline='\n'`/bytes to avoid universal-newline mangling (README §"Diffs with embedded carriage returns") | exposes the marker; parse-level | detects binary entries (`is_binary_file`) | `source_mode/target_mode`, `is_symlink`; rename parsing has open bugs (#119, #77, #113) | **Best parser**, no applier. Good optional dependency for strict parsing/validation; cannot replace the applier |
| **whatthepatch** (cscorley/whatthepatch) | 1.0.7, 2024-11; low activity — README: "has never had much active development … issues may not ever be fixed by the maintainer" <https://github.com/cscorley/whatthepatch>, <https://pypi.org/project/whatthepatch/>, fork statement <https://github.com/kkpattern/whatthepatch> | MIT | yes (many formats) | `apply_diff` (pure Python) or subprocess to GNU `patch(1)` (`use_patch=True`) | **correct** — inserts at `new-1` **[verified]** | partial (context check per change) | loses terminators: works on `splitlines()` output; no CRLF preservation | **broken** — reconstructs `"\n".join(lines) + "\n"` in the subprocess path; pure path drops keepends | no | parses git headers; apply ignores modes | Apply semantics right for zero-context, but line-terminator fidelity and maintenance status fail forge's exactness bar |
| **patch** (techtonik/python-patch) | 1.16, **2016-02** — abandoned <https://pypi.org/project/patch/>, <https://github.com/techtonik/python-patch> | MIT | yes | yes (file-based) | untested; same engine as patch-ng (position = `startsrc`, no offset search) | no (warns "doesn't match", `fuzz` flag skips) | **strips `\r\n` before comparing** — tolerant, not exact | **unimplemented** ("todo \ No newline at end of file" in source) | no | renames/modes crudely | Do not use |
| **patch-ng** (conan-io/python-patch) | 1.19.1, 2026-04; active (Conan's fork) <https://github.com/conan-io/python-patch>, <https://pypi.org/project/patch-ng/> | MIT | yes | yes (file-based) | **BUG: inserts after line 2 for `@@ -3,0 +4,1 @@`, returns success** **[verified]** | no (miscounts not detected; `fuzz` continues on mismatch) | strips `\r\n` before comparing (`patch_ng.py` `patch_stream`/`_match_file_hunks`) | **unimplemented** ("todo" comments) | no | renames via `shutil.move`, `_apply_filemode` | Actively maintained yet **fails the exact-application bar** — evidence that pure-Python appliers are not trustworthy for forge |
| **python-diff** | **does not exist on PyPI** (404 on `/simple/python-diff/`) | — | — | — | — | — | — | — | — | — | Not a candidate |
| **difflib** (stdlib) | maintained with CPython | PSF | produces diffs | n/a (no unified-diff apply) | — | — | — | — | — | — | Only useful to *generate* fixtures |

Conclusions:

1. There is **no maintained pure-Python library that applies unified diffs exactly**
   (byte-faithful terminators, counts, zero-context placement). unidiff is an excellent
   *parser/validator* but has no applier; the two appliers we tested both fail
   zero-context or final-newline fidelity.
2. git itself (via `git apply --check` / `git apply` in a sandbox) is the only
   reference-quality applier available at forge's runtime, and it is already a
   dependency of every forge lane.
3. Whichever route is taken (library, delegation, or fixed in-house applier), the
   differential test strategy below is what makes the choice safe.

---

## Differential test strategy

Goal: **forge accepts exactly the diffs git accepts (modulo declared-strict policy),
and materialized bytes equal git's post-image bytes.** The oracle property:
for every fixture `(base_bytes, diff_bytes)`:
`forge_decides(base, diff) == git_apply_check(base, diff)` under forge's strictness
policy, and when accepted, `sha256(materialized) == sha256(git_apply(base, diff))`.

### 1. Corpus sources

- **git's own apply test suite** (gold standard, permissive-licensed fixtures):
  <https://github.com/git/git/tree/master/t> — mine `t4101-apply-nonl` (no-final-NL),
  `t4102-apply-rename`, `t4103-apply-binary`, `t4104-apply-boundary` (start/end
  anchoring), `t4105-apply-fuzz`, `t4107-apply-ignore-whitespace`, `t4108-apply-threeway`,
  `t4109-apply-multifrag`, `t4112-apply-renames`, `t4113-apply-ending`,
  `t4114-apply-typechange`, `t4117-apply-reject`, `t4118-apply-empty-context`
  (zero-context), `t4124-apply-ws-rule`, `t4126-apply-empty` (empty files),
  `t4129-apply-samemode` (mode-only), `t4130-apply-criss-cross-rename`,
  `t4135-apply-weird-filenames`. Convert each fixture into a `(base, diff, expect)`
  table committed under `tests/data/patch/`.
- **Community diff corpora**: <https://github.com/dcumberland/diff-test-cases>
  ("Examples of file diffs where diff algorithm and options makes a difference") and
  GitHub Desktop's engine suite <https://github.com/desktop/diff-tests>
  ("Reference suite of tests for exercising the diff engine"). Useful for
  realistic line-structure edge cases.
- **Self-generated (property-based)** — the main lever, because corpora above rarely
  cover CRLF/U+2028 combinations: generate a random `old` text, a random edit script
  (insert/delete/replace line spans), render the new text, then produce the diff with
  **git itself** (`git diff --full-index`, plus a `-U0` variant) so the *diff text is
  always well-formed by construction*, then assert:
  1. forge parses+applies and bytes match git's post-image (`git hash-object`);
  2. mutated/negative diffs (truncated bodies, lying counts, swapped `a/`/`b/`,
     missing `diff --git`, reversed hunks, overlapping hunks, markers out of place)
     are rejected with a machine-readable reason and **git agrees** (run
     `git apply --check` on the same mutant — reject/reject parity).

### 2. Mandatory edge-case matrix (acceptance criteria)

Every case must exist as an explicit named test (golden or property):

1. **LF file**, single hunk, context 3 — happy path.
2. **CRLF file committed with CRLF**, diff produced with `core.autocrlf=false` —
   must apply byte-exactly, preserving CR (G6); mixed LF/CRLF in one file must either
   apply exactly or be rejected — never rewritten.
3. **No final newline**: (a) old lacks NL, new keeps it (marker on `-` line only);
   (b) old has NL, new lacks it (marker on `+` line only); (c) both lack it (marker on
   both); (d) last line content otherwise unchanged but NL-ness flips — must be a
   one-line `-`/`+` pair, applied exactly.
4. **Empty file**: create empty (`new file mode`, no hunks / `@@ -0,0 +0,0 @@`),
   delete empty, empty→non-empty (`@@ -0,0 +1,N @@`), non-empty→empty
   (`@@ -1,N +0,0 @@`).
5. **Multiple hunks** in one file: separated, adjacent (touching), at file start
   (`@@ -1,N @@` — must match at beginning), at file end with zero trailing context
   (must match at end), and deliberately overlapping/reordered hunks → reject.
6. **Unicode**: multibyte UTF-8 content; U+2028/U+2029/U+0085/`\f`/`\v` *inside* lines
   (must NOT be treated as line breaks — G6); invalid-UTF-8 bytes must be rejected as
   text-undecodable before hunk matching (forge's `str` pipeline), or handled on
   bytes.
7. **Malformed counts**: header `@@ -1,3 +1,2 @@` with only two old-side body lines;
   counts inconsistent with body (`+`/`-`/context sum) → reject `malformed_diff`
   (git: `corrupt patch at line N`); missing `,count` (defaults to 1) must parse.
8. **Zero-context insertion after line 3**: `@@ -3,0 +4,1 @@` on a 4-line base →
   `line3` stays before `INSERTED` (G1); likewise `@@ -0,0 +1,N @@` (insert at BOF)
   and `@@ -N,0 +M,0 @@` variants.
9. **Whitespace traps**: trailing-whitespace-only additions (never auto-fixed;
   `--whitespace=fix` is forbidden), blank lines at EOF, tabs vs spaces in context.
10. **Structural rejections**: `GIT binary patch`, `Binary files … differ`, renames,
    copies, typechange (symlink↔file), mode-only changes — each a distinct
    machine-readable reason (G7), including the R09 rule that mode-only changes are
    **rejected, not dropped**.
11. **Patch-file-level hazards**: patch itself missing the final newline
    (git errors — <https://stackoverflow.com/questions/18142870/git-error-fatal-corrupt-patch-at-line-36>),
    CRLF inside structural lines (`--- /dev/null\r`, G6), quoted/unicode paths
    (`core.quotePath`), paths with spaces, `a/`-prefix-less diffs (`-p0`).

### 3. Running `git apply --check` as oracle from pytest, safely

Isolation recipe (each test gets a disposable repo and a *config-free* git):

```python
# tests/conftest.py
import subprocess, pytest

def _run(*args, cwd, check=True, input_bytes=None):
    return subprocess.run(args, cwd=cwd, input=input_bytes,
                          capture_output=True, check=check, timeout=30)

@pytest.fixture
def git_workspace(tmp_path, monkeypatch):
    """Hermetic git workspace: no user/system config, no autocrlf, binary-safe."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))   # empty
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")                   # git >= 2.32
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))                     # /etc-style fallbacks
    (tmp_path / "home").mkdir()
    repo = tmp_path / "repo"; repo.mkdir()
    _run("git", "init", "-q", "-b", "main", cwd=repo)
    _run("git", "-C", str(repo), "config", "core.autocrlf", "false")
    _run("git", "-C", str(repo), "config", "core.filemode", "true")
    _run("git", "-C", str(repo), "config", "user.email", "forge@example.invalid")
    _run("git", "-C", str(repo), "config", "user.name", "forge")
    return repo

def write_patch(tmp_path, diff_bytes):
    # ALWAYS binary: text mode would translate \n and destroy CRLF/marker fixtures
    p = tmp_path / "candidate.diff"
    p.write_bytes(diff_bytes)
    return p
```

Key rules (justifications: env vars
<https://git-scm.com/docs/git-config>; `tmp_path` per-test temp dir
<https://docs.pytest.org/en/stable/how-to/tmp_path.html>; flags
<https://git-scm.com/docs/git-apply>):

- **Neutralize config**: `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM`/`GIT_CONFIG_NOSYSTEM`
  (otherwise a developer's `core.autocrlf=true` silently changes oracle results — G6).
- **Seed the base as a blob, exactly like production**: write base bytes with
  `git hash-object -w --stdin` (or `git update-index --add --cacheinfo`), commit, then
  `git apply --check --whitespace=nowarn candidate.diff` inside the workspace. To check
  against the *attempt-base blobs* rather than a worktree, build the index directly:
  `git read-tree <tree>` + `git apply --check --cached`.
- **Never pass the patch through a pipe or text file**: write bytes, use `-i`-style
  file argument; compare results by `git hash-object` of the post-image, never by
  re-reading with text semantics.
- Assert on `returncode` **and** stderr patterns
  (`corrupt patch at line`, `patch failed:`, `patch does not apply`,
  `already exists`, `No valid patches in input` — the last needs `--allow-empty` or
  an upfront emptiness check).
- Keep `subprocess` timeouts and `check=False` for the *negative* cases (non-zero exit
  is the expected outcome).
- Alternative without a repo for pure worktree checks: `git apply --check
  --directory=<root>` outside a repository is limited (no `--index`/`--3way`), so the
  temp-repo route above is preferred; it is also closer to production semantics.

---

## Recommended design for forge (R08/R09)

### D1. Replace the flat `ChangeManifestEntry` with a discriminated union

One entry = one file operation with an explicit precondition and an explicit
**intended result digest**. Discriminate on `kind` (pydantic
`Literal` + tagged union; forge already uses pydantic in `forge/config.py`):

```
ChangeEntry =
  | CreateFile     { path, new_content, mode, intended_digest }
  | DeleteFile     { path, base_blob_digest }
  | FullReplacement{ path, base_blob_digest, new_content, mode, intended_digest }
  | UnifiedPatch   { path, base_blob_digest, hunks: tuple[Hunk, ...], intended_digest }

Hunk = { old_start, old_count, new_start, new_count,      # counts REQUIRED (R09)
         lines: tuple[HunkLine, ...] }
```

- `base_blob_digest` = git blob OID (or sha256, see OQ2) of the authoritative
  attempt-base blob for `path`. `materialize()` **verifies it before applying**;
  mismatch → reject `stale_base` (never "apply anyway") — this is the ADR-0016
  "nothing here trusts the harness's claims" rule carried into the entry level.
- `intended_digest` = digest of the **resulting** content the entry claims to produce.
  After application, materialize recomputes and compares. This converts every
  remaining parser/applier bug from *silent corruption* into a *detected rejection* —
  the direct structural fix for R08: with an explicit `FullReplacement` variant, an
  UPDATE can never be represented as "modify with `new_content` and empty hunks";
  `UnifiedPatch` with zero hunks is malformed (`empty_modify`) and
  `FullReplacement` is the only carrier of full content.
- `old_count`/`new_count` are mandatory on `Hunk` and cross-checked against the body
  (`"-" + " "` == `old_count`, `"+" + " "` == `new_count`, accounting for the
  no-newline marker) before application; git does the same and calls deviations
  `corrupt patch` (G3). Placement uses G1: slice index
  `old_count == 0 ? old_start : old_start - 1`.

### D2. Applier: delegate acceptance to git, materialize in process (hybrid)

Recommendation: **hybrid**.

1. **In-process apply** stays, because `materialize()` works on authoritative blobs in
   memory (ADR-0016), not on a worktree; but it is rewritten to:
   - split diff text on `"\n"` only (never `str.splitlines()`), or parse bytes with
     `surrogateescape`; CR is content (G6);
   - enforce counts (D1), the G1 placement rule, exact positional matching with
     **no offsets and no fuzz** (stricter than git — a deliberate policy, now stated
     in the rejection reason, e.g. `patch_does_not_apply: hunk would apply at offset N`);
   - track `ends_with_newline` explicitly on old/new sides (G5) and verify the old
     side's claim against the base blob bytes before applying;
   - verify `intended_digest` afterwards (D1).
2. **`git apply --check` as an independent acceptance gate** in CI differential tests
   (mandatory) and, cheaply, at runtime when a scratch repo/index is already available:
   seed base blobs (`hash-object -w` + `update-index`), run
   `git apply --check --whitespace=nowarn [--unidiff-zero if zero-context is accepted]`.
   Never `--3way`, never `--reject`, never `--whitespace=fix` (G4, G3).
   If only one of the two can exist, prefer the in-process applier (blobs in memory)
   **plus** the intended-digest check — the digest makes correctness *verifiable*
   even without the oracle.

A pure `git apply` delegation (apply for real in a temp worktree, then read back) is
the simplest correct engine, but it moves byte-fidelity into a subprocess for every
modify and complicates the 400 000-char materialization cap and the
publish-only-what-was-validated boundary; the hybrid keeps the trust boundary in
process with git as differential oracle.

### D3. Rejection policy (per acceptance criterion "reject whole operation with reason")

| Situation | Decision | Reason string |
|---|---|---|
| Binary delta (`GIT binary patch`, `Binary files … differ`) | reject whole bundle | `binary_not_supported` (unchanged) |
| Rename / copy | reject whole bundle | `rename_not_supported` (v0.4 TODO: decompose to Delete+Create only with matching digests) |
| Typechange (symlink↔file, submodule) | reject whole bundle | `typechange_not_supported` |
| **Mode-only change** (`old mode`/`new mode`, no hunks) | **reject whole bundle — never silently drop** (R09) | `mode_change_not_supported` |
| Zero-context hunks | accept, applied with G1 rule (and `--unidiff-zero` if delegated); candidate for a follow-up "require -U3" tightening | — |
| Hunks that would apply only with offset/fuzz/whitespace-fix | reject (strict policy), include the offset in the message | `patch_does_not_apply` |
| Counts inconsistent with body | reject | `malformed_diff` |
| `base_blob_digest` mismatch | reject | `stale_base` |
| Applied result ≠ `intended_digest` | reject | `result_digest_mismatch` |
| Empty `UnifiedPatch` / modify without hunks and without full content | reject | `empty_modify` |

Atomicity mirrors git (G4): one bad entry rejects the whole bundle; there is no
partial materialization and no `.rej` equivalent.

### D4. Effort note

The parser/applier is ~200 lines; the fix is small, the tests are the deliverable.
Keep `unidiff` (MIT) as an *optional* cross-check dependency only if the team prefers
not to own the parser; it does not remove the differential suite.

---

## Open questions

1. **Zero-context acceptance policy.** Capture contract is
   `git diff --binary --full-index <base>` (default `-U3`), so well-formed bundles
   should never contain `-U0` hunks. Should forge reject all zero-context hunks as
   `context_required` (simplest, matches "context-free patches are discouraged",
   G2) or support them via G1/`--unidiff-zero`? (Recommended: reject in v0.3, support
   correctly in the applier anyway for defense in depth.)
2. **Digest identity.** `base_blob_digest` as git blob OID (aligns with
   `attempt_base_oid`, sha1 vs sha256 repo differences) vs plain sha256 of content
   (repo-agnostic)? The choice affects the `intended_digest` comparison and the
   publisher's journal.
3. **Renames in v0.4.** Decompose `rename` into Delete+Create with a similarity note,
   or add a native `RenameEntry` once the Commits-API/GraphQL write path supports it?
   GitHub `createCommitOnBranch` file changes have no rename action (forge research:
   `docs/research/2026-09-13-github-api.md`), so decomposition is the likely path; review-side
   rename detection must then be preserved for diff noise reasons.
4. **Mode changes.** The Commits API write path has no mode action (current docstring
   rationale). Is `mode_change_not_supported` a permanent policy or until the GraphQL
   path gains mode support? If a lane emits mode-only changes routinely (e.g. +x on
   scripts), forge needs a sanctioned way for the planner to re-express them as
   content changes.
5. **Offset policy.** Current code is stricter than git (no offsets). If strictness
   causes frequent spurious rejections of LLM-generated-but-valid diffs, do we report
   the git-computed offset in the rejection message only (informational), or adopt
   git's bounded offset search with digest verification? (Recommended: message-only.)
6. **unidiff as dependency.** Vendor a strict parser (~150 lines, counts + markers +
   paths) vs add `unidiff` (MIT, active) to `THIRD_PARTY_NOTICES.md`? unidiff covers
   counts/path/mode parsing but forge still needs its own applier and byte-level
   splitting.
7. **Runtime oracle cost.** Should `git apply --check` run in production per bundle
   (needs a scratch git dir per materialization; measurable subprocess overhead) or
   only in CI differential tests? Digest verification (D1) may make the runtime
   oracle redundant.

---

## Appendix: local verification commands (2026-09-17)

```bash
# G1/G2: zero-context placement — forge (wrong), plain git apply (EOF), --unidiff-zero (right)
python - <<'PY'
import sys; sys.path.insert(0, 'src')
from forge.runs.candidate import parse_unified_diff
base = 'line1\nline2\nline3\nline4\n'
diff = 'diff --git a/f.txt b/f.txt\nindex 111..222 100644\n--- a/f.txt\n+++ b/f.txt\n@@ -3,0 +4,1 @@\n+INSERTED\n'
out = parse_unified_diff(diff, 'base').materialize({'f.txt': base})
print(repr(out[0].new_content))   # 'line1\nline2\nINSERTED\nline3\nline4\n'  <- BUG
PY
# git oracle:
git init -q repo && cd repo && printf 'line1\nline2\nline3\nline4\n' > f.txt
git add f.txt && git -c user.email=t@t -c user.name=t commit -qm base
printf 'line1\nline2\nline3\nINSERTED\nline4\n' > f.txt && git diff -U0 > z.diff
git checkout -q -- f.txt && git apply z.diff && cat f.txt              # INSERTED at EOF (!)
git checkout -q -- f.txt && git apply --unidiff-zero z.diff && cat f.txt  # correct

# R08: UPDATE with new_content + empty hunks silently materializes to ORIGINAL
# R09 counts: '@@ -1,2 +1,1 @@' with a 1-line body silently drops base line 2
# (see reproduction snippets in the R08/R09 ticket; both confirmed)

# patch-ng 1.19.1 has the same zero-context bug (reports success):
python -m venv v && ./v/bin/pip -q install patch-ng && ./v/bin/python - <<'PY'
import io, tempfile, os
from patch_ng import PatchSet
d = tempfile.mkdtemp(); open(os.path.join(d, 'f.txt'), 'wb').write(b'line1\nline2\nline3\nline4\n')
ps = PatchSet(io.BytesIO(b'--- a/f.txt\n+++ b/f.txt\n@@ -3,0 +4,1 @@\n+INSERTED\n'))
print(ps.apply(root=d), open(os.path.join(d, 'f.txt'), 'rb').read())
# True b'line1\nline2\nINSERTED\nline3\nline4\n'  <- same BUG as forge
PY

# Counts: git rejects lying counts
git apply --check bad.diff   # -> error: corrupt patch at line 9
```

### Source index

- POSIX diff (empty range semantics): <https://pubs.opengroup.org/onlinepubs/9799919799/utilities/diff.html>
- git-apply docs (flags, atomicity, unidiff-zero, whitespace): <https://git-scm.com/docs/git-apply>
- git apply.c (matching, counts, markers, headers, errors): <https://github.com/git/git/blob/master/apply.c>
- CRLF roundtrip commit: <https://github.com/git/git/commit/c24f3abac>
- CRLF `/dev/null\r` fix: <https://public-inbox.org/git/xmqqoau6hz1t.fsf@gitster.dls.corp.google.com/T>
- Incomplete-line parsing commit (mirror): <https://gitlab.com/Minion3665/git/-/commit/3a4eb5ad2e9166255d5921196470710523f24ec4>
- "corrupt patch" symptom: <https://stackoverflow.com/questions/18142870/git-error-fatal-corrupt-patch-at-line-36>
- GNU patch CRLF behavior: <https://unix.stackexchange.com/questions/239364/how-to-fix-hunk-1-failed-at-1-different-line-endings-message>
- git apply test suite: <https://github.com/git/git/tree/master/t>
- unidiff: <https://github.com/matiasb/python-unidiff>, <https://pypi.org/project/unidiff/>, releases <https://packagetrack.dev/pypi/unidiff>, issues #119/#120/#113/#77
- whatthepatch: <https://github.com/cscorley/whatthepatch>, <https://pypi.org/project/whatthepatch/>, maintenance note <https://github.com/kkpattern/whatthepatch>
- python-patch: <https://github.com/techtonik/python-patch>, <https://pypi.org/project/patch/>
- patch-ng: <https://github.com/conan-io/python-patch>, <https://pypi.org/project/patch-ng/>
- Diff corpora: <https://github.com/dcumberland/diff-test-cases>, <https://github.com/desktop/diff-tests>
- pytest tmp_path: <https://docs.pytest.org/en/stable/how-to/tmp_path.html>; git config env isolation: <https://git-scm.com/docs/git-config>
