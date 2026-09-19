"""The 14 bounded delivery-cohort tasks (A17, P2).

A cohort task is a FIXTURE REPO SEED plus PREDECLARED, MECHANICAL acceptance
checks — never the agent's self-reported success. Every task carries a unique
unit id so receipts, attempts and verdicts are joinable across ledger, export
and report. The axes follow the external review's proposed set: create,
update, repair, large file, monorepo scope, tests-only, infra failure,
cancel mid-run. This is the proposed cohort for harness comparison, not a
measured calibration: what makes two passes comparable is that BOTH run the
same tasks under the SAME verification contract (these checks), whatever the
implementing harness is.

Checks are ``argv`` lists executed with ``cwd`` = the candidate worktree;
the literal ``"{python}"`` expands to the running interpreter at check time.
Every check fails on the task's own seed (the work is not done yet) and
passes on correct work — that property is pinned by the unit tests.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "ACCEPTANCE_TIMEOUT_S",
    "AXES",
    "CONTRACT_VERSION",
    "COHORT_TASKS",
    "LARGE_FILE_BYTES",
    "PROCEDURAL_AXES",
    "TASKS_BY_ID",
    "AcceptanceCheck",
    "CohortTask",
    "SeedFile",
    "materialize_seed",
    "task_seed_size",
    "validate_cohort",
]

#: The predeclared verification contract version — recorded in every ledger
#: and report; two passes are only comparable when this string matches.
CONTRACT_VERSION = "forge.delivery-cohort/1"

#: The eight review axes. One task belongs to exactly one axis.
AXES: tuple[str, ...] = (
    "create",
    "update",
    "repair",
    "large-file",
    "monorepo",
    "tests-only",
    "infra-failure",
    "cancel-mid-run",
)

#: Per-check wall-clock bound (a check is a tiny mechanical command, not a
#: test suite run under load).
ACCEPTANCE_TIMEOUT_S = 60.0

#: Files at or above this size classify the task as a large-file task.
LARGE_FILE_BYTES = 100_000


@dataclass(frozen=True)
class SeedFile:
    """One file of the fixture repo seed (deterministic bytes only)."""

    path: str
    content: str
    #: ``content * repeat`` — how the >100KB seeds stay in-repo without
    #: checked-in blobs. Pure repetition keeps the byte count and the
    #: error/word counts exactly predictable, which the oracle checks rely on.
    repeat: int = 1

    @property
    def size(self) -> int:
        return len(self.content.encode("utf-8")) * self.repeat


@dataclass(frozen=True)
class AcceptanceCheck:
    """One predeclared mechanical check: exit 0 in the worktree == pass."""

    name: str
    #: argv run from the candidate worktree root; ``"{python}"`` expands to
    #: the running interpreter. Checks never read agent output — only the
    #: worktree the candidate produced.
    argv: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class CohortTask:
    """One bounded cohort unit: seed + issue text + acceptance checks."""

    unit_id: str
    axis: str
    title: str
    issue_body: str
    seed_files: tuple[SeedFile, ...]
    checks: tuple[AcceptanceCheck, ...]
    #: Monorepo axis: the path prefixes the change is allowed to touch. The
    #: guard itself is one of the checks (sha-pinned untouched files).
    path_scope: tuple[str, ...] = field(default=())
    #: Extra operator procedure beyond the standard drive (infra-failure and
    #: cancel-mid-run units): injected mid-run, not part of the task text.
    procedure: str = ""


# ---------------------------------------------------------------------------
# Seeds (file contents as constants so the sha-pinned scope guards and the
# test-time "checks fail on the seed" probe read the same bytes).
# ---------------------------------------------------------------------------

_CU01_APP = '''"""CU-01 seed application entrypoint."""


def main() -> str:
    return "cohort-seed"
'''

_CU03_SLUGIFY = '''"""Slugify (CU-03 seed: underscores pass through)."""


def slugify(text: str) -> str:
    return text.strip().lower().replace(" ", "-")
'''

_CU04_SETTINGS = "DEFAULT_TIMEOUT_S = 30\n"

_CU04_CLIENT = '''"""CU-04 seed HTTP client stub."""

from settings import DEFAULT_TIMEOUT_S


def attempt(endpoint: str) -> str:
    return f"GET {endpoint} timeout={DEFAULT_TIMEOUT_S}"
'''

_CU05_TEMPERATURE = '''"""Temperature conversions (CU-05 seed: celsius_to_fahrenheit regressed)."""


def celsius_to_fahrenheit(c: float) -> float:
    return c * 9 / 5 + 30


def fahrenheit_to_celsius(f: float) -> float:
    return (f - 32) * 5 / 9
'''

_CU05_TEST = '''"""CU-05 seed: the failing contract test. Fix the CODE, never this file."""

import unittest

from temperature import celsius_to_fahrenheit


class TemperatureTest(unittest.TestCase):
    def test_freezing(self):
        self.assertAlmostEqual(celsius_to_fahrenheit(0), 32)

    def test_boiling(self):
        self.assertAlmostEqual(celsius_to_fahrenheit(100), 212)

    def test_body_temperature(self):
        self.assertAlmostEqual(celsius_to_fahrenheit(37), 98.6)


if __name__ == "__main__":
    unittest.main()
'''

_CU06_VALIDATE = '''"""Email validation (CU-06 seed: rejects every dotted domain)."""


def is_valid_email(value: str) -> bool:
    if value.count("@") != 1:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and domain.isalpha()
'''

_CU06_TEST = '''"""CU-06 seed: the failing contract test. Fix the CODE, never this file."""

import unittest

from email_validate import is_valid_email


class EmailTest(unittest.TestCase):
    def test_plain_address_is_valid(self):
        self.assertTrue(is_valid_email("user@example.com"))

    def test_subdomain_is_valid(self):
        self.assertTrue(is_valid_email("user@mail.example.org"))

    def test_missing_at_is_invalid(self):
        self.assertFalse(is_valid_email("user-at-example.org"))

    def test_double_at_is_invalid(self):
        self.assertFalse(is_valid_email("a@@b.com"))


if __name__ == "__main__":
    unittest.main()
'''

# CU-07: 46 bytes/line x 3200 lines = 147,456 bytes; exactly 3200 ERROR lines
# (one per repetition) — the oracle count the check recomputes from the file.
_CU07_LOG_LINE = "INFO heartbeat service=alpha\nERROR disk-full on /var\n"
_CU07_LOG_REPEAT = 3200

_CU07_ANALYZE = '''"""Events-log analytics (CU-07 seed: reads only the first 100 lines)."""


def count_errors(path: str) -> int:
    with open(path, encoding="utf-8") as handle:
        head = handle.readlines()[:100]
    return sum(1 for line in head if line.startswith("ERROR"))
'''

# CU-08: 36 bytes/line x 3000 lines = 108,000 bytes; 9000 words, of which the
# ones starting with "al" number exactly 6000 — recomputed by the oracle.
_CU08_DICT_LINE = "alpha beta gamma delta epsilon zeta\n"
_CU08_DICT_REPEAT = 3000

_CU08_LOOKUP = '''"""Word-list helpers (CU-08 seed)."""

_WORDS: list[str] = []


def _load(path: str) -> list[str]:
    if not _WORDS:
        with open(path, encoding="utf-8") as handle:
            _WORDS.extend(handle.read().split())
    return _WORDS


def lookup(path: str, word: str) -> bool:
    return word in _load(path)
'''

_CU09_PRICE = '''"""Billing prices (CU-09 seed)."""

PRICE_CENTS = {"espresso": 250, "latte": 380}


def price_cents(item: str) -> int:
    return PRICE_CENTS[item]
'''

_CU09_RATE = '''"""Shipping rates (CU-09 seed: OUT OF SCOPE — must stay byte-identical)."""

FLAT_CENTS = 500


def shipping_cents(subtotal_cents: int) -> int:
    return FLAT_CENTS if subtotal_cents else 0
'''

_CU10_BANNER = '''"""CU-10 seed CLI banner (must route through the shared helper)."""


def banner(name: str) -> str:
    return f"== {name} =="
'''

_CU10_SHOUT = '''"""Shared string helpers (CU-10 seed)."""


def shout(text: str) -> str:
    return text.upper()
'''

_CU10_LEGACY = '''"""Vendored legacy (CU-10 seed: OUT OF SCOPE — must stay byte-identical)."""


def legacy_banner(name: str) -> str:
    return f"== {name} =="
'''

_CU11_STRINGUTIL = '''"""String helpers — CU-11 seed: documented behaviour, zero tests."""


def squeeze(text: str) -> str:
    """Collapse every whitespace run into one space; strip both ends."""
    return " ".join(text.split())


def is_palindrome(text: str) -> bool:
    """True when *text* equals its reversal (exact characters, no folding)."""
    return text == text[::-1]


def truncate(text: str, limit: int) -> str:
    """At most *limit* leading characters; a negative limit yields ''."""
    return text[:limit] if limit >= 0 else ""
'''

_CU12_RATIOS = '''"""Ratio helpers — CU-12 seed: documented behaviour, zero tests."""


def parse_ratio(text: str) -> tuple[float, float]:
    """Parse ``a:b`` into floats; ValueError for any other shape."""
    left, sep, right = text.partition(":")
    if not sep or not left or not right:
        raise ValueError(f"not a ratio: {text!r}")
    return float(left), float(right)


def ratio(text: str) -> float:
    """``a:b`` as a float; ZeroDivisionError propagates when b == 0."""
    a, b = parse_ratio(text)
    return a / b
'''

_CU13_README = "# CU-13 seed\n\nA lab repo instance for the infra-failure unit.\n"

_CU14_README = "# CU-14 seed\n\nA lab repo instance for the cancel-mid-run unit.\n"


def _sha(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The cohort — 14 units, the review axes, every check failing on its seed.
# ---------------------------------------------------------------------------

COHORT_TASKS: tuple[CohortTask, ...] = (
    CohortTask(
        unit_id="CU-01-create-greeting",
        axis="create",
        title="Cohort CU-01: add a greeting module",
        issue_body=(
            "Create `greeting.py` in the repo root exposing `greet(name: str) -> str` "
            "returning exactly `Hello, <name>!` (period-free, name interpolated verbatim). "
            "Stdlib only, no other file may change.\n\n"
            "**Acceptance (mechanical, predeclared):** `greet('World') == 'Hello, World!'` "
            "and `greet('forge') == 'Hello, forge!'`."
        ),
        seed_files=(
            SeedFile("README.md", "# CU-01 seed\n\nCreate greeting.py per the task.\n"),
            SeedFile("app.py", _CU01_APP),
        ),
        checks=(
            AcceptanceCheck(
                name="greet-contract",
                argv=(
                    "{python}",
                    "-c",
                    "from greeting import greet\n"
                    "assert greet('World') == 'Hello, World!', greet('World')\n"
                    "assert greet('forge') == 'Hello, forge!', greet('forge')\n",
                ),
                description="greet() returns the exact documented string",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-02-create-calculator",
        axis="create",
        title="Cohort CU-02: add a calculator module",
        issue_body=(
            "Create `calculator.py` in the repo root exposing `add(a: float, b: float) -> float` "
            "and `mul(a: float, b: float) -> float`. Stdlib only, no other file may change.\n\n"
            "**Acceptance (mechanical, predeclared):** `add(2, 3) == 5`, `mul(3, 4) == 12`."
        ),
        seed_files=(SeedFile("README.md", "# CU-02 seed\n\nCreate calculator.py per the task.\n"),),
        checks=(
            AcceptanceCheck(
                name="calculator-contract",
                argv=(
                    "{python}",
                    "-c",
                    "from calculator import add, mul\n"
                    "assert add(2, 3) == 5, add(2, 3)\n"
                    "assert mul(3, 4) == 12, mul(3, 4)\n",
                ),
                description="add/mul return the documented sums/products",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-03-update-slugify",
        axis="update",
        title="Cohort CU-03: slugify must fold underscores",
        issue_body=(
            "`slugify.py::slugify` currently lowercases and maps spaces to hyphens but leaves "
            "underscores untouched. Change it so underscores ALSO become hyphens; space and "
            "strip behaviour stays exactly as it is. Only `slugify.py` may change.\n\n"
            "**Acceptance (mechanical, predeclared):** `slugify('Hello World') == 'hello-world'`, "
            "`slugify('foo_bar') == 'foo-bar'`, `slugify('A B_C') == 'a-b-c'`."
        ),
        seed_files=(SeedFile("slugify.py", _CU03_SLUGIFY),),
        checks=(
            AcceptanceCheck(
                name="slugify-contract",
                argv=(
                    "{python}",
                    "-c",
                    "from slugify import slugify\n"
                    "assert slugify('Hello World') == 'hello-world', slugify('Hello World')\n"
                    "assert slugify('foo_bar') == 'foo-bar', slugify('foo_bar')\n"
                    "assert slugify('A B_C') == 'a-b-c', slugify('A B_C')\n",
                ),
                description="underscores fold to hyphens; existing behaviour preserved",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-04-update-retry-config",
        axis="update",
        title="Cohort CU-04: thread MAX_RETRIES through the client",
        issue_body=(
            "Add `MAX_RETRIES = 3` to `settings.py`, then extend `client.py::attempt` so its "
            "return value ends with ` retries=3` (reading the constant, not a literal): "
            "`attempt('/x') == 'GET /x timeout=30 retries=3'`. Only `settings.py` and "
            "`client.py` may change.\n\n"
            "**Acceptance (mechanical, predeclared):** the constant exists with value 3 and "
            "the `attempt` string carries it."
        ),
        seed_files=(SeedFile("settings.py", _CU04_SETTINGS), SeedFile("client.py", _CU04_CLIENT)),
        checks=(
            AcceptanceCheck(
                name="retry-config-contract",
                argv=(
                    "{python}",
                    "-c",
                    "import settings\n"
                    "from client import attempt\n"
                    "assert settings.MAX_RETRIES == 3, settings.MAX_RETRIES\n"
                    "assert attempt('/x') == 'GET /x timeout=30 retries=3', attempt('/x')\n",
                ),
                description="MAX_RETRIES constant exists and attempt() reports it",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-05-repair-temperature",
        axis="repair",
        title="Cohort CU-05: fix the failing temperature test",
        issue_body=(
            "`test_temperature.py` fails. `celsius_to_fahrenheit` is wrong — fix the CODE in "
            "`temperature.py` until the test passes. Editing the test file is out of contract "
            "and fails acceptance.\n\n"
            "**Acceptance (mechanical, predeclared):** `python -m unittest -v test_temperature` "
            "exits 0 in the worktree."
        ),
        seed_files=(
            SeedFile("temperature.py", _CU05_TEMPERATURE),
            SeedFile("test_temperature.py", _CU05_TEST),
        ),
        checks=(
            AcceptanceCheck(
                name="unittest-temperature",
                argv=("{python}", "-m", "unittest", "-v", "test_temperature"),
                description="the predeclared failing suite passes against the repaired code",
            ),
            AcceptanceCheck(
                name="test-file-untouched",
                argv=(
                    "{python}",
                    "-c",
                    f"import hashlib\n"
                    f"body = open('test_temperature.py', 'rb').read()\n"
                    f"assert hashlib.sha256(body).hexdigest() == '{_sha(_CU05_TEST)}'\n",
                ),
                description="the contract test file stayed byte-identical",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-06-repair-email",
        axis="repair",
        title="Cohort CU-06: fix the email validator regression",
        issue_body=(
            "`test_email_validate.py` fails: dotted domains are rejected. Fix the CODE in "
            "`email_validate.py` so ordinary dotted domains validate while the documented "
            "negatives stay rejected. Editing the test file is out of contract.\n\n"
            "**Acceptance (mechanical, predeclared):** `python -m unittest -v test_email_validate` "
            "exits 0 in the worktree."
        ),
        seed_files=(
            SeedFile("email_validate.py", _CU06_VALIDATE),
            SeedFile("test_email_validate.py", _CU06_TEST),
        ),
        checks=(
            AcceptanceCheck(
                name="unittest-email-validate",
                argv=("{python}", "-m", "unittest", "-v", "test_email_validate"),
                description="the predeclared failing suite passes against the repaired code",
            ),
            AcceptanceCheck(
                name="test-file-untouched",
                argv=(
                    "{python}",
                    "-c",
                    f"import hashlib\n"
                    f"body = open('test_email_validate.py', 'rb').read()\n"
                    f"assert hashlib.sha256(body).hexdigest() == '{_sha(_CU06_TEST)}'\n",
                ),
                description="the contract test file stayed byte-identical",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-07-large-log-analyze",
        axis="large-file",
        title="Cohort CU-07: count ERROR lines in a 147KB log",
        issue_body=(
            "`analyze.py::count_errors` reads only the first 100 lines of `events.log`. Fix it "
            "to count `ERROR`-prefixed lines across the WHOLE file (line-at-a-time streaming is "
            "fine; the file is ~147KB). Only `analyze.py` may change.\n\n"
            "**Acceptance (mechanical, predeclared):** `count_errors('events.log')` equals an "
            "independent recount over the raw file."
        ),
        seed_files=(
            SeedFile("events.log", _CU07_LOG_LINE, repeat=_CU07_LOG_REPEAT),
            SeedFile("analyze.py", _CU07_ANALYZE),
        ),
        checks=(
            AcceptanceCheck(
                name="count-errors-matches-oracle",
                argv=(
                    "{python}",
                    "-c",
                    "from analyze import count_errors\n"
                    "raw = sum(\n"
                    "    1 for line in open('events.log', encoding='utf-8')\n"
                    "    if line.startswith('ERROR')\n"
                    ")\n"
                    "got = count_errors('events.log')\n"
                    "assert got == raw, (got, raw)\n",
                ),
                description="agent's count equals the independent raw-file recount",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-08-large-dictionary",
        axis="large-file",
        title="Cohort CU-08: prefix counts over a 108KB word list",
        issue_body=(
            "`dictionary.txt` holds one space-separated word line repeated thousands of times. "
            "Extend `lookup.py` with `count_prefix(path: str, prefix: str) -> int` returning the "
            "number of words starting with *prefix* (case-sensitive). `lookup` keeps working. "
            "Only `lookup.py` may change.\n\n"
            "**Acceptance (mechanical, predeclared):** `count_prefix('dictionary.txt', 'al')` "
            "equals an independent recount over the raw file."
        ),
        seed_files=(
            SeedFile("dictionary.txt", _CU08_DICT_LINE, repeat=_CU08_DICT_REPEAT),
            SeedFile("lookup.py", _CU08_LOOKUP),
        ),
        checks=(
            AcceptanceCheck(
                name="count-prefix-matches-oracle",
                argv=(
                    "{python}",
                    "-c",
                    "from lookup import count_prefix, lookup\n"
                    "raw = sum(\n"
                    "    1 for word in open('dictionary.txt', encoding='utf-8').read().split()\n"
                    "    if word.startswith('al')\n"
                    ")\n"
                    "got = count_prefix('dictionary.txt', 'al')\n"
                    "assert got == raw, (got, raw)\n"
                    "assert lookup('dictionary.txt', 'alpha') is True\n",
                ),
                description="prefix count equals the independent recount; lookup intact",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-09-monorepo-billing-scope",
        axis="monorepo",
        title="Cohort CU-09: billing discount, shipping out of scope",
        issue_body=(
            "Monorepo with `services/billing` and `services/shipping`. Add "
            "`discount_cents(subtotal_cents: int) -> int` to `services/billing/price.py` "
            "returning the integer FLOOR of 10% of the subtotal. The `services/shipping` "
            "tree is OUT OF SCOPE: any byte changed there fails acceptance.\n\n"
            "**Acceptance (mechanical, predeclared):** `discount_cents(380) == 38`, "
            "`discount_cents(0) == 0`, `discount_cents(1) == 0`; `services/shipping/rate.py` "
            "sha-identical to the seed and `shipping_cents(380) == 500`."
        ),
        seed_files=(
            SeedFile("services/billing/price.py", _CU09_PRICE),
            SeedFile("services/shipping/rate.py", _CU09_RATE),
        ),
        path_scope=("services/billing/",),
        checks=(
            AcceptanceCheck(
                name="billing-discount-contract",
                argv=(
                    "{python}",
                    "-c",
                    "from services.billing.price import discount_cents\n"
                    "assert discount_cents(380) == 38, discount_cents(380)\n"
                    "assert discount_cents(0) == 0, discount_cents(0)\n"
                    "assert discount_cents(1) == 0, discount_cents(1)\n",
                ),
                description="10% floor discount implemented in billing",
            ),
            AcceptanceCheck(
                name="shipping-bytes-untouched",
                argv=(
                    "{python}",
                    "-c",
                    f"import hashlib\n"
                    f"body = open('services/shipping/rate.py', 'rb').read()\n"
                    f"assert hashlib.sha256(body).hexdigest() == '{_sha(_CU09_RATE)}'\n",
                ),
                description="out-of-scope shipping module stayed byte-identical",
            ),
            AcceptanceCheck(
                name="shipping-behavior-intact",
                argv=(
                    "{python}",
                    "-c",
                    "from services.shipping.rate import shipping_cents\n"
                    "assert shipping_cents(380) == 500\n"
                    "assert shipping_cents(0) == 0\n",
                ),
                description="out-of-scope shipping behaviour unchanged",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-10-monorepo-shared-banner",
        axis="monorepo",
        title="Cohort CU-10: CLI banner via the shared package",
        issue_body=(
            "Monorepo with `apps/cli`, `packages/shared` and a vendored `vendor/` tree. Change "
            "`apps/cli/main.py::banner` so the name is uppercased through "
            "`packages/shared/strings.py::shout` (`banner('ada') == '== ADA =='`). You may touch "
            "`apps/` and `packages/` only; `vendor/` is OUT OF SCOPE.\n\n"
            "**Acceptance (mechanical, predeclared):** the banner contract holds and "
            "`vendor/legacy.py` stays sha-identical to the seed."
        ),
        seed_files=(
            SeedFile("apps/cli/main.py", _CU10_BANNER),
            SeedFile("packages/shared/strings.py", _CU10_SHOUT),
            SeedFile("vendor/legacy.py", _CU10_LEGACY),
        ),
        path_scope=("apps/", "packages/"),
        checks=(
            AcceptanceCheck(
                name="banner-contract",
                argv=(
                    "{python}",
                    "-c",
                    "from apps.cli.main import banner\n"
                    "assert banner('ada') == '== ADA ==', banner('ada')\n"
                    "assert banner('') == '==  ==', banner('')\n",
                ),
                description="banner uppercases through the shared helper",
            ),
            AcceptanceCheck(
                name="vendor-bytes-untouched",
                argv=(
                    "{python}",
                    "-c",
                    f"import hashlib\n"
                    f"body = open('vendor/legacy.py', 'rb').read()\n"
                    f"assert hashlib.sha256(body).hexdigest() == '{_sha(_CU10_LEGACY)}'\n",
                ),
                description="out-of-scope vendored module stayed byte-identical",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-11-tests-stringutil",
        axis="tests-only",
        title="Cohort CU-11: write the missing stringutil tests",
        issue_body=(
            "`stringutil.py` implements `squeeze`, `is_palindrome` and `truncate` exactly as "
            "their docstrings say, and has ZERO tests. Write `test_stringutil.py` (unittest, "
            "repo root) covering: squeeze on a multi-space run and on leading/trailing spaces; "
            "is_palindrome true AND false; truncate cutting at the limit AND the negative-limit "
            "empty result. Do not modify `stringutil.py`.\n\n"
            "**Acceptance (mechanical, predeclared):** the suite exits 0 and references all "
            "three documented functions."
        ),
        seed_files=(SeedFile("stringutil.py", _CU11_STRINGUTIL),),
        checks=(
            AcceptanceCheck(
                name="unittest-stringutil",
                argv=("{python}", "-m", "unittest", "-v", "test_stringutil"),
                description="the new suite passes against the untouched code",
            ),
            AcceptanceCheck(
                name="covers-documented-functions",
                argv=(
                    "{python}",
                    "-c",
                    "src = open('test_stringutil.py', encoding='utf-8').read()\n"
                    "missing = [n for n in ('squeeze', 'is_palindrome', 'truncate') if n not in src]\n"
                    "assert not missing, missing\n",
                ),
                description="every documented function is exercised by name",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-12-tests-ratios",
        axis="tests-only",
        title="Cohort CU-12: write the missing ratios tests",
        issue_body=(
            "`ratios.py` implements `parse_ratio` (ValueError on every non-`a:b` shape) and "
            "`ratio` (ZeroDivisionError propagates). Write `test_ratios.py` (unittest, repo "
            "root) covering: the happy parse, at least two malformed shapes raising ValueError, "
            "the computed ratio, and `ratio('1:0')` raising ZeroDivisionError. Do not modify "
            "`ratios.py`.\n\n"
            "**Acceptance (mechanical, predeclared):** the suite exits 0 and exercises both "
            "functions and both exception types."
        ),
        seed_files=(SeedFile("ratios.py", _CU12_RATIOS),),
        checks=(
            AcceptanceCheck(
                name="unittest-ratios",
                argv=("{python}", "-m", "unittest", "-v", "test_ratios"),
                description="the new suite passes against the untouched code",
            ),
            AcceptanceCheck(
                name="covers-documented-behaviours",
                argv=(
                    "{python}",
                    "-c",
                    "src = open('test_ratios.py', encoding='utf-8').read()\n"
                    "missing = [\n"
                    "    n for n in ('parse_ratio', 'ratio', 'ValueError', 'ZeroDivisionError')\n"
                    "    if n not in src\n"
                    "]\n"
                    "assert not missing, missing\n",
                ),
                description="both functions and both exception types are exercised by name",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-13-infra-retry",
        axis="infra-failure",
        title="Cohort CU-13: env report (with an injected lane death)",
        issue_body=(
            "Create `env_report.py` in the repo root exposing `python_report() -> dict` with "
            "keys `python` (interpreter version string) and `platform` (OS name). Stdlib only, "
            "no other file may change.\n\n"
            "**Acceptance (mechanical, predeclared):** both keys present, both non-empty "
            "strings.\n\n"
            "NOTE for the operator: this unit's lane is deliberately killed mid-run once; the "
            "unit is delivered by the RETRY, and BOTH attempts (the dead one included) stay in "
            "the ledger with their spend."
        ),
        seed_files=(SeedFile("README.md", _CU13_README),),
        procedure=(
            "After `/go` is consumed and the lane starts, kill the lane run once (cancel the "
            "Actions run / fail the job). Let forge Tier-1 auto-revive (or `/retry` if it "
            "parks fatal). Record the dead attempt AND the retry attempt; accept only on the "
            "retry's checks."
        ),
        checks=(
            AcceptanceCheck(
                name="env-report-contract",
                argv=(
                    "{python}",
                    "-c",
                    "from env_report import python_report\n"
                    "report = python_report()\n"
                    "assert isinstance(report.get('python'), str) and report['python']\n"
                    "assert isinstance(report.get('platform'), str) and report['platform']\n",
                ),
                description="python/platform keys present and non-empty",
            ),
        ),
    ),
    CohortTask(
        unit_id="CU-14-cancel-mid-run",
        axis="cancel-mid-run",
        title="Cohort CU-14: CSV export (cancelled by the operator mid-run)",
        issue_body=(
            "Create `metrics_export.py` in the repo root exposing "
            "`export_rows(rows) -> str` rendering name/value pairs as CSV with header "
            "`name,value` (csv module; LF line endings). Stdlib only, no other file may "
            "change.\n\n"
            "**Acceptance (mechanical, predeclared):** "
            "`export_rows([('a', 1), ('b', 2)]) == 'name,value\\na,1\\nb,2\\n'`.\n\n"
            "NOTE for the operator: this unit is CANCELLED mid-run on purpose — it is never "
            "accepted; its spend stays in the cohort totals as wasted spend and never enters "
            "the accepted denominator."
        ),
        seed_files=(SeedFile("README.md", _CU14_README),),
        procedure=(
            "Post `/implement`, approve with `/go`, then post `/cancel <run-id>` while the "
            "lane is still running. The unit's verdict is `cancelled` BY CONTRACT — record "
            "the attempt and its usage, run no acceptance verdict of `accepted`."
        ),
        checks=(
            AcceptanceCheck(
                name="csv-export-contract",
                argv=(
                    "{python}",
                    "-c",
                    "from metrics_export import export_rows\n"
                    "got = export_rows([('a', 1), ('b', 2)])\n"
                    "assert got == 'name,value\\na,1\\nb,2\\n', repr(got)\n",
                ),
                description="CSV rendering matches the documented bytes",
            ),
        ),
    ),
)

TASKS_BY_ID: dict[str, CohortTask] = {task.unit_id: task for task in COHORT_TASKS}

#: Units whose contract verdict is decided by procedure, not by checks alone.
PROCEDURAL_AXES: frozenset[str] = frozenset({"infra-failure", "cancel-mid-run"})


def task_seed_size(task: CohortTask) -> int:
    """Total materialized seed size in bytes."""
    return sum(seed_file.size for seed_file in task.seed_files)


def materialize_seed(task: CohortTask, dest: Path) -> list[Path]:
    """Write the fixture seed into *dest* (created); returns the file paths."""
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for seed_file in task.seed_files:
        target = dest / seed_file.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(seed_file.content * seed_file.repeat, encoding="utf-8")
        written.append(target)
    return written


def validate_cohort() -> None:
    """Structural integrity of the cohort definition (raises ValueError).

    Pins the review's contract: 14 bounded tasks, every review axis present,
    unique unit ids, every task carrying at least one mechanical check and a
    non-empty issue body, no seed path escaping the worktree, and each of the
    two large-file units seeded at or above the 100KB threshold.
    """
    if len(COHORT_TASKS) != 14:
        raise ValueError(f"cohort must hold exactly 14 tasks, has {len(COHORT_TASKS)}")
    seen: set[str] = set()
    axes_seen: set[str] = set()
    for task in COHORT_TASKS:
        if task.unit_id in seen:
            raise ValueError(f"duplicate unit id {task.unit_id!r}")
        seen.add(task.unit_id)
        if task.axis not in AXES:
            raise ValueError(f"{task.unit_id}: unknown axis {task.axis!r}")
        axes_seen.add(task.axis)
        if not task.checks:
            raise ValueError(f"{task.unit_id}: no acceptance checks (predeclared contract)")
        names = {check.name for check in task.checks}
        if len(names) != len(task.checks):
            raise ValueError(f"{task.unit_id}: duplicate check names")
        if not task.issue_body.strip():
            raise ValueError(f"{task.unit_id}: empty issue body")
        for seed_file in task.seed_files:
            path = Path(seed_file.path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{task.unit_id}: seed path escapes worktree: {seed_file.path}")
    missing_axes = set(AXES) - axes_seen
    if missing_axes:
        raise ValueError(f"axes without a task: {sorted(missing_axes)}")
    for unit_id in ("CU-07-large-log-analyze", "CU-08-large-dictionary"):
        size = task_seed_size(TASKS_BY_ID[unit_id])
        if size < LARGE_FILE_BYTES:
            raise ValueError(f"{unit_id}: seed is {size} bytes, expected >= {LARGE_FILE_BYTES}")
