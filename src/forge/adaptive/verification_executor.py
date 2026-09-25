"""R37-15 (#296) — verification in a SEPARATE trusted executor against
BUILT candidate artifacts and REAL dependencies.

The tested-world twin (``system_verification.py``, #278 — kept as the
DETERMINISTIC REFERENCE) proved the machinery in-process: real sqlite
DDL/DML, synthetic services, in-process delivery. The review's three
remaining gaps, closed here:

- **A separate trusted executor, verified from the LAUNCHED PROCESS.**
  :class:`VerificationExecutor` launches the verification as a REAL
  subprocess (``python -m forge.adaptive.verification_executor
  --candidate-set <doc> --out <report>``) under a SCRUBBED environment:
  an explicit :data:`DEFAULT_ENV_ALLOWLIST` — no model keys, no
  provider tokens, no publication credentials — and the launch
  receipt records the exact env keys present. Isolation is then
  PROVED BY PROBE from inside the launched process, never by DTO
  fields: safe DENY PROBES attempt (a) an HTTP request to a sentinel
  endpoint reachable only with egress/proxy credentials and (b) a
  provider-API-shaped call whose token must be absent. A 2xx answer
  to either is an ``isolation_violation`` and the whole verification
  fails closed — the probes have teeth because a wrongly allowed
  credential actually reaches the endpoint and is caught.
- **Built artifacts, not models.** The tested services are REAL
  INSTALLABLE WHEELS (the two fixture projects under
  ``evaluation/tested_world/`` — ``orders-api`` and
  ``orders-projection``, each with a pyproject, a contract module and
  its own tests). The executor verifies each wheel file's sha256
  against the digest FROZEN in the candidate set, installs those
  exact bytes into its own venv, runs the wheels' OWN contract tests
  and drives the integration scenario by IMPORTING THE INSTALLED
  MODULES — never the in-repo source. The tested-world identity binds
  the BUILT wheel digests (a changed-source rebuild is a different
  digest, a different world, selective invalidation through the
  existing machinery; hatchling builds are deterministic, so an
  unchanged-source rebuild reproduces the same digest and correctly
  invalidates nothing).
- **Real dependency scenarios.** The sqlite upgrade leg runs INSIDE
  the executor against the INSTALLED wheel's schema code (seeded
  baseline, preservation fingerprints, new-shape writes). The broker
  arm is a REAL network round trip: a local socket-based fake broker
  subprocess (an asyncio TCP server — ``--serve-broker`` below) that
  the executor dials over TCP, with DUPLICATE DELIVERY INJECTED AT THE
  SOCKET, while the INSTALLED consumer (its own subprocess from the
  venv) applies its idempotent handler against real sqlite
  transactions. Idempotency is asserted from BOTH sides: the broker's
  journal (deliveries/acks seen on the wire) and the consumer's own
  durable rows.

R38-07 (#308) tightened the ISOLATION CLAIMS to match what is actually
observable (a dead endpoint never "proves" containment):

- **The five-outcome probe taxonomy.** Every controlled probe lands in
  exactly one of :data:`PROBE_OUTCOMES` —
  ``authorized_control_succeeded`` / ``expected_denial_observed`` /
  ``unavailable`` / ``inconclusive`` / ``violation``. Network
  exceptions and 5xx answers are ``unavailable`` (the endpoint could
  not demonstrate anything), timeouts are ``inconclusive``, an
  auth-shaped denial (401/403/407) counts as
  ``expected_denial_observed`` ONLY while the class's positive control
  is green, and a 2xx answer to credential material is a
  ``violation``. The old ``denied-refused``/``denied-other-status``
  buckets — which let a dead endpoint masquerade as a successful
  denial — are GONE.
- **The positive control.** Each probe class first dials a SECOND
  route on the same test server that answers 200 WITHOUT credentials
  (the liveness control); the deny probe runs only when that control
  is green. A pass therefore requires, per class: the control alive
  AND one ``expected_denial_observed`` AND zero violations — anything
  less renders the isolation ``unproven`` with the named blocker, and
  a 503/DNS failure can NEVER yield a pass.
- **Clean HOME, narrowed PATH.** The subprocess does NOT inherit the
  parent HOME: the launcher provisions an isolated empty HOME and a
  PATH reduced to the system minimum plus the resolved venv tooling.
  A planted credential file under the parent HOME (``~/.netrc``,
  ``~/.config/...`` shapes) is unreadable from the verifier, asserted
  by a probe reading the process's OWN HOME shapes. The launcher
  records the whole configuration as the enforcement profile
  (:class:`EnforcementProfile`, ``verification.enforcement_profile_digest``)
  and the launched process VALIDATES the declaration against what it
  actually observes.
- **Three separate verdicts, never one ``isolation: true``.** The
  authority receipt reports ``environment_hygiene`` /
  ``credential_non_disclosure`` / ``network_enforcement`` as three
  DISTINCT fields plus the tri-state ``isolation``
  (``proven``/``unproven``/``violated``).
- **No first-surviving-key heuristics.** The deny probes present an
  explicit SYNTHETIC qualification credential
  (:data:`SYNTHETIC_PROBE_CREDENTIAL`) — never an ambient key. The
  environment scan's only job is the ABSENCE assertion: any
  credential-shaped survivor is a hygiene violation that fails the
  run closed.
- **Reference coverage, labeled.** The fixture TCP-broker and sqlite
  legs are labeled ``reference-coverage`` in the environment profile
  and the report — they are NOT production RabbitMQ/PostgreSQL proof.

The report keeps every existing discipline: per-edge results with the
executor receipt (launch env keys, deny-probe outcomes, installed
wheel digests, command exits, log digests), the
``verification.report_coverage`` fragment, evidence records bound to
the frozen world, selective invalidation keyed on the built-wheel
digests, and readiness/merge/deploy as THREE DISTINCT booleans — a
green verification grants neither merge nor deployment.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from forge.adaptive.models import CandidateSet, CandidateSetMember
from forge.adaptive.system_verification import (
    EdgeResult,
    SystemEdge,
    SystemReadiness,
    _report_coverage,
    system_readiness,
)
from forge.adaptive.verification_sets import (
    DependencyIdentity,
    EvidenceLedger,
    EvidenceRecord,
    freeze_tested_world,
    record_evidence,
)

__all__ = [
    "AUTHORITY_RECEIPT_SCHEMA",
    "CANDIDATE_BUNDLE_SCHEMA",
    "CREDENTIAL_HOME_SHAPES",
    "CREDENTIAL_SOURCE_SYNTHETIC",
    "DEFAULT_ENV_ALLOWLIST",
    "DEFAULT_ENV_ALLOWLIST_VERSION",
    "ENFORCEMENT_PROFILE_SCHEMA",
    "EXECUTOR_REPORT_SCHEMA",
    "EXECUTOR_RECEIPT_SCHEMA",
    "HOME_POLICY",
    "ISOLATION_PROVEN",
    "ISOLATION_UNPROVEN",
    "ISOLATION_VIOLATED",
    "NETWORK_POLICY_CLASS",
    "OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED",
    "OUTCOME_EXPECTED_DENIAL_OBSERVED",
    "OUTCOME_INCONCLUSIVE",
    "OUTCOME_UNAVAILABLE",
    "OUTCOME_VIOLATION",
    "PATH_POLICY",
    "PROBE_OUTCOMES",
    "REDELIVERY_OUTCOME_SCHEMA",
    "SYSTEM_PATH_ENTRIES",
    "SYNTHETIC_PROBE_CREDENTIAL",
    "AuthorityReceipt",
    "CandidateBundle",
    "CommandRun",
    "ControlledDenyProbe",
    "EnforcementProfile",
    "ExecutorReceipt",
    "ExecutorReport",
    "ExecutorRun",
    "InstalledWheel",
    "IsolationUnproven",
    "IsolationViolated",
    "ProbeAttempt",
    "ProbeTarget",
    "VerificationExecutor",
    "WheelRef",
    "executor_candidate_set",
    "executor_edges",
    "executor_readiness",
    "introspect_and_probe",
    "main",
    "narrowed_path_entries",
    "require_isolated",
    "run_deny_probe",
    "run_positive_control",
    "run_probe_class",
    "scan_credential_env",
    "scan_home_credentials",
    "scrubbed_environment",
]

#: The report's versioned schema discriminator.
EXECUTOR_REPORT_SCHEMA = "forge.executor.verification/1"

#: The ``verification.authority_receipt`` fragment's discriminator —
#: the isolation proof PRODUCED BY THE LAUNCHED PROCESS.
AUTHORITY_RECEIPT_SCHEMA = "forge.verification.authority-receipt/1"

#: The executor receipt's discriminator — the launch/install/command
#: evidence every per-edge result carries.
EXECUTOR_RECEIPT_SCHEMA = "forge.verification.executor-receipt/1"

#: The ``dependency.redelivery_outcome`` fragment's discriminator.
REDELIVERY_OUTCOME_SCHEMA = "forge.dependency.redelivery-outcome/1"

#: The candidate-bundle document's discriminator (the file the parent
#: hands the subprocess).
CANDIDATE_BUNDLE_SCHEMA = "forge.executor.candidate-bundle/1"

#: The enforcement profile's discriminator — the isolation
#: configuration the LAUNCHER used (env allowlist version, HOME/PATH
#: policy, network policy class). Claims are scoped to exactly this
#: profile; the launched process validates the declaration against
#: what it actually observes.
ENFORCEMENT_PROFILE_SCHEMA = "forge.verification.enforcement-profile/1"

#: The explicit environment ALLOWLIST. The subprocess env is built by
#: intersecting the parent env with these names ONLY: no model keys,
#: no provider tokens, no publication credentials, no proxy/egress
#: configuration. Anything not listed is DROPPED (that is the scrub),
#: and the launch receipt records the exact keys that remained.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "HOME",
    "LANG",
    "LC_ALL",
    "PATH",
    "TMPDIR",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
)

#: The allowlist's version stamp — the enforcement profile binds the
#: allowlist CONTENT and this version; changing the allowlist without
#: changing the version is a profile drift the digest catches anyway.
DEFAULT_ENV_ALLOWLIST_VERSION = "env-allowlist/1"

#: What counts as a credential-shaped environment key for the ABSENCE
#: assertion: anything whose NAME looks like a token, key, secret,
#: password, credential or proxy variable. R38-07 removed the old
#: first-surviving-key presentation heuristic: the scan's ONLY job now
#: is to prove absence — any survivor is a hygiene violation that
#: fails the run closed. The deny probes present the explicit
#: :data:`SYNTHETIC_PROBE_CREDENTIAL` instead, never an ambient key.
CREDENTIAL_ENV_PATTERN = re.compile(
    r"(TOKEN|KEY|SECRET|PASSWORD|PASSWD|CREDENTIAL|PROXY|_PAT\b)", re.IGNORECASE
)

#: The credential-shaped HOME entries the verifier's own HOME probe
#: checks: a planted file under the PARENT home in any of these shapes
#: must be UNREADABLE from the verifier (the clean-HOME guarantee).
CREDENTIAL_HOME_SHAPES: tuple[str, ...] = (
    ".netrc",
    ".git-credentials",
    ".config/git/credentials",
    ".aws/credentials",
    ".docker/config.json",
    ".kube/config",
)

#: The PATH policy: the executable search path is reduced to the
#: system minimum (existence-filtered) plus the resolved venv tooling
#: — the parent's full PATH never reaches the subprocess.
SYSTEM_PATH_ENTRIES: tuple[str, ...] = (
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
PATH_POLICY = "system-minimum/1"

#: The clean-HOME policy stamp (an isolated empty HOME provisioned by
#: the launcher; the layout digest is part of the profile).
HOME_POLICY = "clean-home/1"

#: The network policy class the probes qualify: egress is deny-by-
#: default and every enforcement claim is probe-gated by a live
#: positive control.
NETWORK_POLICY_CLASS = "probe-gated-deny-default/1"

#: The explicit SYNTHETIC credential the deny probes present — created
#: for qualification only. It is deliberately NOT a valid credential
#: for any real surface: an endpoint that answers 2xx to it does not
#: gate, and that is a ``violation``. The probe NEVER picks an ambient
#: key to present.
SYNTHETIC_PROBE_CREDENTIAL = "forge-synthetic-probe-token-not-a-real-credential"
CREDENTIAL_SOURCE_SYNTHETIC = "synthetic-qualification-sentinel"

#: The five-outcome taxonomy for controlled probes (R38-07): a dead or
#: unhealthy endpoint is ``unavailable`` — never a successful denial.
OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED = "authorized_control_succeeded"
OUTCOME_EXPECTED_DENIAL_OBSERVED = "expected_denial_observed"
OUTCOME_UNAVAILABLE = "unavailable"
OUTCOME_INCONCLUSIVE = "inconclusive"
OUTCOME_VIOLATION = "violation"
PROBE_OUTCOMES: tuple[str, ...] = (
    OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED,
    OUTCOME_EXPECTED_DENIAL_OBSERVED,
    OUTCOME_UNAVAILABLE,
    OUTCOME_INCONCLUSIVE,
    OUTCOME_VIOLATION,
)

#: The tri-state isolation verdict: ``violated`` (credential material
#: actually reached something — fail closed, exit 3), ``proven`` (the
#: positive controls were alive, every probe class observed its
#: expected denial, zero violations, clean HOME/PATH) or ``unproven``
#: (a prerequisite could not be demonstrated — exit 5, never a pass).
ISOLATION_PROVEN = "proven"
ISOLATION_UNPROVEN = "unproven"
ISOLATION_VIOLATED = "violated"

#: Exit codes the executor subprocess reports (documented contract):
#: 0 clean, 2 usage error, 3 isolation violated, 4 verification
#: failed, 5 isolation unproven (a probe prerequisite — control
#: endpoint, HOME, PATH — could not be demonstrated; the verification
#: is BLOCKED, never turned green).
EXIT_CLEAN = 0
EXIT_USAGE = 2
EXIT_ISOLATION_VIOLATED = 3
EXIT_VERIFICATION_FAILED = 4
EXIT_ISOLATION_UNPROVEN = 5

#: Environment keys platform spawn layers may inject into any child
#: regardless of what the launcher passed (macOS adds
#: ``__CF_USER_TEXT_ENCODING``) — tolerated by the membership check,
#: never by the credential-shape scan.
PLATFORM_ENV_TOLERANCE: frozenset[str] = frozenset({"__CF_USER_TEXT_ENCODING"})

#: The executor world's fixed membership (mirrors the reference twin):
#: two changed wheel-backed services, one pinned ledger baseline, two
#: external dependency pins.
PRODUCER_SERVICE = "orders-api"
CONSUMER_SERVICE = "orders-projection"
PINNED_SERVICE = "ledger-baseline"
DB_DEPENDENCY = "orders-db"
BUS_DEPENDENCY = "orders-bus"

#: The shared message dialects the fixture wheels speak (the same
#: widening the reference twin models: v2 adds ``region``).
DIALECT_FIELDS: dict[str, tuple[str, ...]] = {
    "v1": ("id", "total"),
    "v2": ("id", "total", "region"),
}

#: The broker topic the producer publishes on (the fixture contract's).
ORDERS_TOPIC = "orders.events.order-created"

#: The default scenario shape (the twin's volumes, trimmed for the
#: socket replay): how many messages the broker arm publishes, how
#: many rows the seeded baseline carries, how many baseline rows the
#: projection leg replays.
SCENARIO_MESSAGES = 8
SCENARIO_SEED_ROWS = 25
SCENARIO_BASELINE_ROWS = 15

#: How many times the fake broker delivers each message on the socket
#: (the duplicate injected AT THE SOCKET).
DUPLICATE_DELIVERIES = 2

_CONTRACT_BUNDLE_INPUT_SCHEMA = "forge.executor.contract-bundle/1"
_TEST_BUNDLE_INPUT_SCHEMA = "forge.executor.test-bundle/1"
_ENVIRONMENT_PROFILE_INPUT_SCHEMA = "forge.executor.environment-profile/1"


# ---------------------------------------------------------------------------
# Deterministic helpers (hex-only seeds, the harness convention).
# ---------------------------------------------------------------------------


def _hex64(seed: str) -> str:
    """A lowercase 64-hex sha256 shape derived from *seed*."""
    return (seed * 64)[:64]


def _image_digest(seed: str) -> str:
    """A sha256-prefixed image digest, as registries spell them."""
    return f"sha256:{_hex64(seed)}"


def _oid40(seed: str) -> str:
    """A lowercase 40-hex git sha shape derived from *seed*."""
    return (seed * 40)[:40]


def _canonical_digest(payload: object) -> str:
    """sha256 over the canonical JSON of *payload* (sorted keys)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# The environment scrub and the deny probes (fail closed, from the
# launched process).
# ---------------------------------------------------------------------------


def scrubbed_environment(
    source: Mapping[str, str],
    allowlist: Sequence[str] = DEFAULT_ENV_ALLOWLIST,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the subprocess environment from an EXPLICIT allowlist.

    Every parent variable NOT named in *allowlist* is dropped — that is
    the scrub. *extra* rides through verbatim (harness wiring the
    caller owns); the launch receipt records it as an explicit
    addition, and the deny probes remain the backstop that catches a
    credential riding it.
    """
    env = {name: source[name] for name in allowlist if name in source}
    if extra:
        env.update(dict(extra))
    return env


def scan_credential_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """The credential-shaped variables THIS process can see (by name).

    R38-07: this is the ABSENCE assertion only — an empty mapping is
    the proof; ANY survivor is a hygiene violation that fails the run
    closed (the probe never presents a survivor; the deny probes
    present :data:`SYNTHETIC_PROBE_CREDENTIAL` instead).
    """
    source = os.environ if env is None else env
    return {
        name: str(source[name]) for name in sorted(source) if CREDENTIAL_ENV_PATTERN.search(name)
    }


def scan_home_credentials(home: str | None = None) -> dict[str, str]:
    """The credential-shaped HOME entries THIS process can read.

    The verifier's own HOME probe: a planted credential file under the
    PARENT home in any :data:`CREDENTIAL_HOME_SHAPES` shape must be
    UNREADABLE from the verifier — under the clean-HOME policy this
    scan finds NOTHING, and any hit is a hygiene violation.
    """
    base = Path(home if home is not None else os.environ.get("HOME", ""))
    if not str(base):
        return {}
    return {
        shape: str(base / shape) for shape in CREDENTIAL_HOME_SHAPES if (base / shape).is_file()
    }


def narrowed_path_entries(*, extra_dirs: Sequence[str] = ()) -> tuple[str, ...]:
    """The subprocess PATH: the system minimum plus explicit tool dirs.

    The parent's full executable search path NEVER reaches the
    subprocess: the policy is a fixed system minimum (existence
    filtered, order preserved) plus the directories the launcher
    explicitly resolved — typically the venv tooling (``uv``).
    """
    entries: list[str] = []
    for entry in (*SYSTEM_PATH_ENTRIES, *extra_dirs):
        if entry and entry not in entries and Path(entry).is_dir():
            entries.append(entry)
    return tuple(entries)


@dataclass(frozen=True)
class ProbeAttempt:
    """One HTTP leg of a controlled probe, classified by the taxonomy.

    ``outcome`` is one of :data:`PROBE_OUTCOMES`; ``status_code`` is
    ``None`` for transport failures (no answer exists to cite).
    """

    outcome: str
    status_code: int | None
    detail: str

    def as_document(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "status_code": self.status_code,
            "detail": self.detail,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ProbeAttempt:
        return cls(
            outcome=str(document.get("outcome", OUTCOME_INCONCLUSIVE)),
            status_code=document.get("status_code"),
            detail=str(document.get("detail", "")),
        )


def run_positive_control(name: str, url: str, *, timeout: float = 10.0) -> ProbeAttempt:
    """Dial the POSITIVE CONTROL: a route that answers 200 WITHOUT
    credentials, proving the endpoint is alive and reachable.

    A 2xx is ``authorized_control_succeeded`` — and nothing else is: a
    non-2xx answer is ``unavailable`` (the control did not demonstrate
    aliveness) and a timeout is ``inconclusive``. The deny probe for
    the class runs ONLY when this control is green, so a dead or
    misrouted control can never let a denial claim through.
    """
    try:
        response = httpx.get(url, timeout=timeout, trust_env=True)
    except httpx.TimeoutException as error:
        return ProbeAttempt(
            OUTCOME_INCONCLUSIVE, None, f"the control timed out: {type(error).__name__}: {error}"
        )
    except httpx.HTTPError as error:
        return ProbeAttempt(
            OUTCOME_UNAVAILABLE,
            None,
            f"the control could not be reached: {type(error).__name__}: {error}",
        )
    if response.status_code // 100 == 2:
        return ProbeAttempt(
            OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED,
            response.status_code,
            f"the control answered {response.status_code} without credentials — the endpoint is alive",
        )
    return ProbeAttempt(
        OUTCOME_UNAVAILABLE,
        response.status_code,
        f"the control answered {response.status_code} — the endpoint's aliveness is not demonstrated",
    )


def run_deny_probe(
    name: str,
    url: str,
    header: str,
    *,
    credential: str = SYNTHETIC_PROBE_CREDENTIAL,
    prefix: str = "",
    timeout: float = 10.0,
    control_green: bool = False,
) -> ProbeAttempt:
    """Attempt ONE credentialed reach from THIS process — expect denial.

    The probe presents the EXPLICIT synthetic credential (never an
    ambient key — the first-surviving-key heuristic is gone): the
    environment scan is a separate ABSENCE assertion. Classification
    (the five-outcome taxonomy):

    - a 2xx answer → ``violation``: credential material reached
      something it never should have (an endpoint that serves the
      synthetic token does not gate);
    - 401/403/407 with the positive control green →
      ``expected_denial_observed`` — the only outcome that counts as
      a denial OBSERVED;
    - any 5xx, or a transport failure (refused, DNS, reset) →
      ``unavailable`` — a dead endpoint is NOT a denial (the R38-07
      fix: these used to masquerade as ``denied-refused`` /
      ``denied-other-status`` and "prove" isolation);
    - a timeout → ``inconclusive``;
    - anything else → ``inconclusive`` (neither service nor an
      auth-shaped denial).
    """
    headers = {header: f"{prefix}{credential}"} if credential else {}
    try:
        response = httpx.get(url, headers=headers, timeout=timeout, trust_env=True)
    except httpx.TimeoutException as error:
        return ProbeAttempt(
            OUTCOME_INCONCLUSIVE, None, f"the probe timed out: {type(error).__name__}: {error}"
        )
    except httpx.HTTPError as error:
        return ProbeAttempt(
            OUTCOME_UNAVAILABLE,
            None,
            f"the endpoint could not be reached ({type(error).__name__}: {error}) —"
            " unreachability is not a denial",
        )
    code = response.status_code
    if code // 100 == 2:
        return ProbeAttempt(
            OUTCOME_VIOLATION,
            code,
            "the endpoint served the probe's credential material — the isolation did not hold",
        )
    if code >= 500:
        return ProbeAttempt(
            OUTCOME_UNAVAILABLE,
            code,
            f"the endpoint answered {code} (unhealthy) — a failing service is not a denial",
        )
    if code in (401, 403, 407):
        if control_green:
            return ProbeAttempt(
                OUTCOME_EXPECTED_DENIAL_OBSERVED,
                code,
                f"the endpoint refused the probe credential ({code}) with the control green",
            )
        return ProbeAttempt(
            OUTCOME_INCONCLUSIVE,
            code,
            f"the endpoint refused the probe ({code}) but the positive control was not green"
            " — the denial is not evidence",
        )
    return ProbeAttempt(
        OUTCOME_INCONCLUSIVE,
        code,
        f"the endpoint answered {code} — neither service nor an auth-shaped denial",
    )


@dataclass(frozen=True)
class ProbeTarget:
    """One probe class: the gated deny URL plus its positive control."""

    name: str
    deny_url: str
    control_url: str
    header: str
    prefix: str = ""


@dataclass(frozen=True)
class ControlledDenyProbe:
    """One probe class's complete outcome, observed from the launched
    process: the positive-control leg, the deny leg (``None`` when the
    control was not green — a blocked probe is claimed as NOTHING),
    the class verdict in the five-outcome taxonomy and the named
    blocker when the verdict is not ``expected_denial_observed``."""

    name: str
    deny_url: str
    control_url: str
    header: str
    credential_source: str
    presented_from_env: str
    control: ProbeAttempt
    deny: ProbeAttempt | None
    outcome: str
    blocker: str

    def as_document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "deny_url": self.deny_url,
            "control_url": self.control_url,
            "header": self.header,
            "credential_source": self.credential_source,
            "presented_from_env": self.presented_from_env,
            "control": self.control.as_document(),
            "deny": self.deny.as_document() if self.deny is not None else None,
            "outcome": self.outcome,
            "blocker": self.blocker,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ControlledDenyProbe:
        deny = document.get("deny")
        return cls(
            name=str(document["name"]),
            deny_url=str(document.get("deny_url", "")),
            control_url=str(document.get("control_url", "")),
            header=str(document.get("header", "")),
            credential_source=str(document.get("credential_source", CREDENTIAL_SOURCE_SYNTHETIC)),
            presented_from_env=str(document.get("presented_from_env", "")),
            control=ProbeAttempt.from_document(document.get("control", {})),
            deny=None if deny is None else ProbeAttempt.from_document(deny),
            outcome=str(document.get("outcome", OUTCOME_INCONCLUSIVE)),
            blocker=str(document.get("blocker", "")),
        )


def run_probe_class(target: ProbeTarget, *, timeout: float = 10.0) -> ControlledDenyProbe:
    """Run one probe class: the positive control FIRST, the deny probe
    only when the control is green (a blocked class claims nothing)."""
    control = run_positive_control(target.name, target.control_url, timeout=timeout)
    if control.outcome != OUTCOME_AUTHORIZED_CONTROL_SUCCEEDED:
        return ControlledDenyProbe(
            name=target.name,
            deny_url=target.deny_url,
            control_url=target.control_url,
            header=target.header,
            credential_source=CREDENTIAL_SOURCE_SYNTHETIC,
            presented_from_env="",
            control=control,
            deny=None,
            outcome=control.outcome,
            blocker=f"the positive control did not succeed: {control.detail}",
        )
    deny = run_deny_probe(
        target.name,
        target.deny_url,
        target.header,
        prefix=target.prefix,
        timeout=timeout,
        control_green=True,
    )
    blocker = (
        ""
        if deny.outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED
        else f"the deny probe was {deny.outcome}: {deny.detail}"
    )
    return ControlledDenyProbe(
        name=target.name,
        deny_url=target.deny_url,
        control_url=target.control_url,
        header=target.header,
        credential_source=CREDENTIAL_SOURCE_SYNTHETIC,
        presented_from_env="",
        control=control,
        deny=deny,
        outcome=deny.outcome,
        blocker=blocker,
    )


@dataclass(frozen=True)
class EnforcementProfile:
    """The isolation configuration the LAUNCHER used, recorded as the
    claims' scope (``verification.enforcement_profile_digest``).

    The profile binds: the env allowlist (with its version stamp), the
    clean-HOME policy (the exact path and its layout digest), the
    narrowed PATH policy (the exact entries), the caller-owned env
    additions and the network policy class the probes qualify. The
    launched process VALIDATES the declaration against what it
    actually observes (HOME value, PATH entries, env-key membership) —
    a mismatch is a hygiene violation, not a silent pass."""

    schema: str = ENFORCEMENT_PROFILE_SCHEMA
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    env_allowlist_version: str = DEFAULT_ENV_ALLOWLIST_VERSION
    extra_env_keys: tuple[str, ...] = ()
    home_policy: str = HOME_POLICY
    home_path: str = ""
    home_layout: tuple[str, ...] = ()
    path_policy: str = PATH_POLICY
    path_entries: tuple[str, ...] = ()
    network_policy_class: str = NETWORK_POLICY_CLASS

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "env_allowlist": list(self.env_allowlist),
            "env_allowlist_version": self.env_allowlist_version,
            "extra_env_keys": list(self.extra_env_keys),
            "home_policy": self.home_policy,
            "home_path": self.home_path,
            "home_layout": list(self.home_layout),
            "path_policy": self.path_policy,
            "path_entries": list(self.path_entries),
            "network_policy_class": self.network_policy_class,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> EnforcementProfile:
        return cls(
            schema=str(document.get("schema", ENFORCEMENT_PROFILE_SCHEMA)),
            env_allowlist=tuple(document.get("env_allowlist", DEFAULT_ENV_ALLOWLIST)),
            env_allowlist_version=str(
                document.get("env_allowlist_version", DEFAULT_ENV_ALLOWLIST_VERSION)
            ),
            extra_env_keys=tuple(document.get("extra_env_keys", ())),
            home_policy=str(document.get("home_policy", HOME_POLICY)),
            home_path=str(document.get("home_path", "")),
            home_layout=tuple(document.get("home_layout", ())),
            path_policy=str(document.get("path_policy", PATH_POLICY)),
            path_entries=tuple(document.get("path_entries", ())),
            network_policy_class=str(document.get("network_policy_class", NETWORK_POLICY_CLASS)),
        )

    @classmethod
    def digest_document(cls, document: Mapping[str, Any]) -> str:
        """The profile's sha256 over its canonical JSON — the value the
        receipt reports as ``verification.enforcement_profile_digest``."""
        return _canonical_digest(document)

    def digest(self) -> str:
        return self.digest_document(self.as_document())


@dataclass(frozen=True)
class AuthorityReceipt:
    """The isolation proof PRODUCED BY THE LAUNCHED PROCESS.

    The subprocess self-reports: the env keys it actually sees, the
    credential-shaped survivors among them (must be empty), its OWN
    HOME and the credential-shaped shapes found there (must be none),
    its PATH entries, the controlled probes it ran — and THREE
    SEPARATE verdicts (never one ``isolation: true``):
    ``environment_hygiene`` (env scrub + clean HOME + PATH scope),
    ``credential_non_disclosure`` (no credential material worked) and
    ``network_enforcement`` (live positive control + expected denial
    per probe class), plus the tri-state ``isolation`` verdict, the
    fatal ``violations`` and the ``blockers`` that name what prevented
    a proven verdict. The parent's launch receipt
    (:attr:`ExecutorRun.launch_env_keys`) carries the other half —
    what the LAUNCHER passed — so absence is proved from BOTH sides."""

    env_keys: tuple[str, ...] = ()
    allowlist: tuple[str, ...] = ()
    credential_shaped_keys: tuple[str, ...] = ()
    home: str = ""
    home_shapes_checked: tuple[str, ...] = ()
    home_shapes_found: tuple[str, ...] = ()
    path_entries: tuple[str, ...] = ()
    probes: tuple[ControlledDenyProbe, ...] = ()
    environment_hygiene: str = ISOLATION_UNPROVEN
    credential_non_disclosure: str = ISOLATION_UNPROVEN
    network_enforcement: str = ISOLATION_UNPROVEN
    isolation: str = ISOLATION_UNPROVEN
    violations: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()

    @property
    def isolated(self) -> bool:
        """Compat view: only a ``proven`` verdict is isolated — an
        ``unproven`` receipt is NEVER consumable as a pass."""
        return self.isolation == ISOLATION_PROVEN

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": AUTHORITY_RECEIPT_SCHEMA,
            "env_keys": list(self.env_keys),
            "allowlist": list(self.allowlist),
            "credential_shaped_keys": list(self.credential_shaped_keys),
            "home": self.home,
            "home_shapes_checked": list(self.home_shapes_checked),
            "home_shapes_found": list(self.home_shapes_found),
            "path_entries": list(self.path_entries),
            "probes": [probe.as_document() for probe in self.probes],
            "environment_hygiene": self.environment_hygiene,
            "credential_non_disclosure": self.credential_non_disclosure,
            "network_enforcement": self.network_enforcement,
            "isolation": self.isolation,
            "violations": list(self.violations),
            "blockers": list(self.blockers),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> AuthorityReceipt:
        return cls(
            env_keys=tuple(document.get("env_keys", ())),
            allowlist=tuple(document.get("allowlist", ())),
            credential_shaped_keys=tuple(document.get("credential_shaped_keys", ())),
            home=str(document.get("home", "")),
            home_shapes_checked=tuple(document.get("home_shapes_checked", ())),
            home_shapes_found=tuple(document.get("home_shapes_found", ())),
            path_entries=tuple(document.get("path_entries", ())),
            probes=tuple(
                ControlledDenyProbe.from_document(probe) for probe in document.get("probes", ())
            ),
            environment_hygiene=str(document.get("environment_hygiene", ISOLATION_UNPROVEN)),
            credential_non_disclosure=str(
                document.get("credential_non_disclosure", ISOLATION_UNPROVEN)
            ),
            network_enforcement=str(document.get("network_enforcement", ISOLATION_UNPROVEN)),
            isolation=str(document.get("isolation", ISOLATION_UNPROVEN)),
            violations=tuple(document.get("violations", ())),
            blockers=tuple(document.get("blockers", ())),
        )


def introspect_and_probe(
    allowlist: Sequence[str],
    targets: Sequence[ProbeTarget],
    *,
    timeout: float = 10.0,
    declared_profile: Mapping[str, Any] | None = None,
) -> AuthorityReceipt:
    """The launched process's own isolation check (fail closed).

    Three SEPARATE verdicts and a tri-state isolation — an unavailable
    or inconclusive probe class yields ``unproven`` with the named
    blocker (never a pass), and any credential-shaped survivor, any
    readable HOME shape, any PATH/env drift from the declared profile
    or any 2xx answer to probe credential material is a ``violated``
    verdict that fails the whole run closed.
    """
    hygiene: list[str] = []
    credential_keys = tuple(scan_credential_env())
    hygiene.extend(
        f"credential-shaped env key survived the scrub: {key}" for key in credential_keys
    )
    home = os.environ.get("HOME", "")
    home_found = scan_home_credentials(home)
    hygiene.extend(
        f"credential-shaped file readable under the verifier HOME: {shape}"
        for shape in sorted(home_found)
    )
    allowed_names = set(allowlist) | set(PLATFORM_ENV_TOLERANCE)
    if declared_profile is not None:
        allowed_names |= set(declared_profile.get("extra_env_keys", ()))
    hygiene.extend(
        f"env key outside the enforcement profile allowlist: {name}"
        for name in sorted(set(os.environ) - allowed_names)
    )
    observed_path = tuple(entry for entry in os.environ.get("PATH", "").split(os.pathsep) if entry)
    if declared_profile is not None:
        declared_home = str(declared_profile.get("home_path", ""))
        if declared_home and home != declared_home:
            hygiene.append("the verifier HOME is not the enforcement profile's clean home")
        declared_path = set(declared_profile.get("path_entries", ()))
        hygiene.extend(
            f"PATH entry outside the enforcement profile: {entry}"
            for entry in sorted(set(observed_path) - declared_path)
        )

    probes = tuple(run_probe_class(target, timeout=timeout) for target in targets)
    violations = list(hygiene)
    violations.extend(
        f"{probe.name}: {probe.deny.detail if probe.deny else probe.blocker}"
        " — credential material reached the endpoint"
        for probe in probes
        if probe.outcome == OUTCOME_VIOLATION
    )
    blockers: list[str] = []
    if not targets:
        blockers.append(
            "no probe classes were declared — the network enforcement cannot be observed"
        )
    blockers.extend(
        f"{probe.name}: {probe.blocker}"
        for probe in probes
        if probe.outcome != OUTCOME_EXPECTED_DENIAL_OBSERVED
    )

    environment_hygiene = "failed" if hygiene else "passed"
    any_violation = any(probe.outcome == OUTCOME_VIOLATION for probe in probes)
    all_denied = bool(probes) and all(
        probe.outcome == OUTCOME_EXPECTED_DENIAL_OBSERVED for probe in probes
    )
    if any_violation:
        credential_non_disclosure = "violated"
    elif all_denied:
        credential_non_disclosure = "passed"
    else:
        credential_non_disclosure = ISOLATION_UNPROVEN
    network_enforcement = "demonstrated" if all_denied else ISOLATION_UNPROVEN

    if violations:
        isolation = ISOLATION_VIOLATED
    elif (
        environment_hygiene == "passed"
        and credential_non_disclosure == "passed"
        and network_enforcement == "demonstrated"
    ):
        isolation = ISOLATION_PROVEN
    else:
        isolation = ISOLATION_UNPROVEN
    return AuthorityReceipt(
        env_keys=tuple(sorted(os.environ)),
        allowlist=tuple(sorted(set(allowlist))),
        credential_shaped_keys=credential_keys,
        home=home,
        home_shapes_checked=CREDENTIAL_HOME_SHAPES,
        home_shapes_found=tuple(sorted(home_found)),
        path_entries=observed_path,
        probes=probes,
        environment_hygiene=environment_hygiene,
        credential_non_disclosure=credential_non_disclosure,
        network_enforcement=network_enforcement,
        isolation=isolation,
        violations=tuple(violations),
        blockers=tuple(blockers),
    )


# ---------------------------------------------------------------------------
# The candidate bundle (the document the parent hands the subprocess).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WheelRef:
    """One built artifact: the wheel FILE plus its recorded sha256."""

    service: str
    path: Path
    sha256: str

    def as_document(self) -> dict[str, Any]:
        return {"service": self.service, "path": str(self.path), "sha256": self.sha256}


@dataclass(frozen=True)
class CandidateBundle:
    """Everything the executor needs: the frozen candidate set (the
    wheel digests ARE the members' image digests), the wheel files,
    the probe-class endpoints (each gated deny URL plus its
    POSITIVE-CONTROL URL on the same server), the baseline pin's
    served API and the scenario volumes."""

    candidate_set: CandidateSet
    wheels: tuple[WheelRef, ...]
    sentinel_url: str
    provider_url: str
    work_dir: Path
    sentinel_control_url: str = ""
    provider_control_url: str = ""
    probe_timeout: float = 10.0
    contract_document: dict[str, Any] = field(default_factory=dict)
    test_document: dict[str, Any] = field(default_factory=dict)
    environment_document: dict[str, Any] = field(default_factory=dict)
    baseline_api_served: str = "api/v3"
    baseline_api_required: str = "api/v3"
    scenario_messages: int = SCENARIO_MESSAGES
    seed_rows: int = SCENARIO_SEED_ROWS
    baseline_rows: int = SCENARIO_BASELINE_ROWS
    allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": CANDIDATE_BUNDLE_SCHEMA,
            "candidate_set": self.candidate_set.model_dump(),
            "wheels": [wheel.as_document() for wheel in self.wheels],
            "sentinel_url": self.sentinel_url,
            "provider_url": self.provider_url,
            "work_dir": str(self.work_dir),
            "sentinel_control_url": self.sentinel_control_url,
            "provider_control_url": self.provider_control_url,
            "probe_timeout": self.probe_timeout,
            "contract_document": dict(self.contract_document),
            "test_document": dict(self.test_document),
            "environment_document": dict(self.environment_document),
            "baseline_api_served": self.baseline_api_served,
            "baseline_api_required": self.baseline_api_required,
            "scenario_messages": self.scenario_messages,
            "seed_rows": self.seed_rows,
            "baseline_rows": self.baseline_rows,
            "allowlist": list(self.allowlist),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> CandidateBundle:
        return cls(
            candidate_set=CandidateSet.model_validate(document["candidate_set"]),
            wheels=tuple(
                WheelRef(
                    service=str(wheel["service"]),
                    path=Path(str(wheel["path"])),
                    sha256=str(wheel["sha256"]),
                )
                for wheel in document.get("wheels", ())
            ),
            sentinel_url=str(document["sentinel_url"]),
            provider_url=str(document["provider_url"]),
            work_dir=Path(str(document["work_dir"])),
            sentinel_control_url=str(document.get("sentinel_control_url", "")),
            provider_control_url=str(document.get("provider_control_url", "")),
            probe_timeout=float(document.get("probe_timeout", 10.0)),
            contract_document=dict(document.get("contract_document", {})),
            test_document=dict(document.get("test_document", {})),
            environment_document=dict(document.get("environment_document", {})),
            baseline_api_served=str(document.get("baseline_api_served", "api/v3")),
            baseline_api_required=str(document.get("baseline_api_required", "api/v3")),
            scenario_messages=int(document.get("scenario_messages", SCENARIO_MESSAGES)),
            seed_rows=int(document.get("seed_rows", SCENARIO_SEED_ROWS)),
            baseline_rows=int(document.get("baseline_rows", SCENARIO_BASELINE_ROWS)),
            allowlist=tuple(document.get("allowlist", DEFAULT_ENV_ALLOWLIST)),
        )

    def write(self, path: Path) -> Path:
        _write_json(path, self.to_document())
        return path

    @classmethod
    def read(cls, path: Path) -> CandidateBundle:
        return cls.from_document(_read_json(path))

    def probe_targets(self) -> tuple[ProbeTarget, ...]:
        """The probe classes this bundle declares: each gated deny URL
        paired with its positive-control URL (a missing control URL is
        a class that can never be proven — it will surface as the
        named blocker, never as a pass)."""
        return (
            ProbeTarget(
                name="sentinel-egress",
                deny_url=self.sentinel_url,
                control_url=self.sentinel_control_url,
                header="X-Egress-Token",
            ),
            ProbeTarget(
                name="provider-api",
                deny_url=self.provider_url,
                control_url=self.provider_control_url,
                header="Authorization",
                prefix="Bearer ",
            ),
        )


# ---------------------------------------------------------------------------
# The executor world composition (parent side): the frozen identity
# binds the BUILT wheel digests.
# ---------------------------------------------------------------------------


def contract_bundle_document(dialect: str = "v2", topic: str = ORDERS_TOPIC) -> dict[str, Any]:
    """The declared shared contract (the referee the wheels are judged
    against — the executor cross-checks each INSTALLED wheel's own
    declared dialect against this document)."""
    return {
        "schema": _CONTRACT_BUNDLE_INPUT_SCHEMA,
        "topic": topic,
        "dialect": dialect,
        "fields": list(DIALECT_FIELDS[dialect]),
    }


def test_bundle_document(
    messages: int = SCENARIO_MESSAGES,
    seed_rows: int = SCENARIO_SEED_ROWS,
    baseline_rows: int = SCENARIO_BASELINE_ROWS,
    extra_cases: Sequence[str] = (),
) -> dict[str, Any]:
    """The declared test bundle: the wheels' own selftests plus the
    scenario volumes the executor drives."""
    return {
        "schema": _TEST_BUNDLE_INPUT_SCHEMA,
        "suites": {
            "orders-api": ["orders_api.selftest"],
            "orders-projection": ["orders_projection.selftest"],
            "socket-redelivery": {"messages": messages, "duplicates": DUPLICATE_DELIVERIES},
            "db-upgrade": {"seed_rows": seed_rows},
            "baseline-projection": {"rows": baseline_rows},
        },
        "extra_cases": list(extra_cases),
    }


#: The reference-coverage label class: the fixture TCP-broker and
#: sqlite legs prove MECHANICS against local stand-ins, not production
#: RabbitMQ/PostgreSQL behavior. An explicit note in the report, never
#: a removal of the coverage itself.
REFERENCE_COVERAGE = "reference-coverage"

#: The per-dependency reference-coverage notes (R38-07: the fixture
#: broker/sqlite tests are labeled so no report can read them as
#: production-broker/database proof).
DEPENDENCY_COVERAGE_LABELS: dict[str, str] = {
    DB_DEPENDENCY: (
        f"{REFERENCE_COVERAGE}: the sqlite fixture ladder — reference coverage for the"
        " upgrade mechanics, NOT production PostgreSQL proof"
    ),
    BUS_DEPENDENCY: (
        f"{REFERENCE_COVERAGE}: the local socket fake broker — reference coverage for"
        " the redelivery mechanics, NOT production RabbitMQ proof"
    ),
}


def environment_profile_document() -> dict[str, Any]:
    """The declared environment profile: the wheel-backed services, the
    pinned baseline, the two dependency pins — each fixture dependency
    carrying its explicit reference-coverage label."""
    return {
        "schema": _ENVIRONMENT_PROFILE_INPUT_SCHEMA,
        "profile": "python-wheel-executor-v1",
        "services": [
            PRODUCER_SERVICE,
            CONSUMER_SERVICE,
            PINNED_SERVICE,
            DB_DEPENDENCY,
            BUS_DEPENDENCY,
        ],
        "coverage_labels": dict(DEPENDENCY_COVERAGE_LABELS),
    }


def executor_candidate_set(
    *,
    producer_wheel_sha256: str,
    consumer_wheel_sha256: str,
    producer_source_oid: str,
    consumer_source_oid: str,
    ledger_baseline_digest: str | None = None,
    ledger_baseline_oid: str | None = None,
    dialect: str = "v2",
    work_id: str = "wp-executor-orders-1",
    plan_revision: int = 1,
    extra_test_cases: Sequence[str] = (),
) -> CandidateSet:
    """Freeze the executor world: the two BUILT wheels are the changed
    members (their image digests are the wheel sha256s), the ledger
    baseline rides at its PIN, and the bundle/profile digests come from
    the declared documents — all persisted through the existing
    :func:`freeze_tested_world` machinery."""
    contract = contract_bundle_document(dialect)
    tests = test_bundle_document(extra_cases=extra_test_cases)
    environment = environment_profile_document()
    members = (
        CandidateSetMember(
            repository_id=PRODUCER_SERVICE,
            base_oid=_oid40("e01"),
            candidate_oid=producer_source_oid,
            image_digest=f"sha256:{producer_wheel_sha256}",
            role="changed",
        ),
        CandidateSetMember(
            repository_id=CONSUMER_SERVICE,
            base_oid=_oid40("e02"),
            candidate_oid=consumer_source_oid,
            image_digest=f"sha256:{consumer_wheel_sha256}",
            role="changed",
        ),
        CandidateSetMember(
            repository_id=PINNED_SERVICE,
            base_oid=ledger_baseline_oid or _oid40("e03"),
            candidate_oid=ledger_baseline_oid or _oid40("e03"),  # the PIN
            image_digest=ledger_baseline_digest or _image_digest("e13f"),
            role="baseline",
        ),
    )
    return freeze_tested_world(
        CandidateSet(
            work_id=work_id,
            plan_revision=plan_revision,
            work_contract_digest=_hex64("e37a"),
            members=members,
        ),
        contract_bundle_digest=_canonical_digest(contract),
        test_bundle_digest=_canonical_digest(tests),
        environment_profile_digest=_canonical_digest(environment),
        environment_pins={
            DB_DEPENDENCY: _image_digest("edb1"),
            BUS_DEPENDENCY: _image_digest("eb52"),
        },
        policy_refs=("compat/orders-matrix@1",),
    )


def executor_edges() -> tuple[SystemEdge, ...]:
    """The three edges the executor judges (the reference twin's shape,
    member-for-member)."""
    return (
        SystemEdge(
            edge_id=f"contract:{PRODUCER_SERVICE}->{CONSUMER_SERVICE}",
            kind="contract",
            repositories=(PRODUCER_SERVICE, CONSUMER_SERVICE),
            description=(
                "built wheels' own contract tests + the referee dialect check +"
                " the message replay over the socket broker"
            ),
        ),
        SystemEdge(
            edge_id=f"baseline:{CONSUMER_SERVICE}->{PINNED_SERVICE}",
            kind="baseline",
            repositories=(CONSUMER_SERVICE, PINNED_SERVICE),
            description=(
                "installed consumer's projection replayed against rows served by"
                " the PINNED baseline at its pinned digest"
            ),
        ),
        SystemEdge(
            edge_id="environment:integration",
            kind="environment",
            repositories=(PRODUCER_SERVICE, CONSUMER_SERVICE, PINNED_SERVICE),
            description=(
                "sqlite upgrade from the seeded baseline via the installed wheel's"
                " schema code + duplicate redelivery over real sockets"
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Receipts: every command, every install, every log — digested.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandRun:
    """One command the executor ran, with its exit code and the sha256
    of its captured log (stdout+stderr) — the receipt never keeps only
    the happy summary."""

    name: str
    argv: tuple[str, ...]
    exit_code: int
    log_sha256: str
    duration_ms: int

    def as_document(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "argv": list(self.argv),
            "exit_code": self.exit_code,
            "log_sha256": self.log_sha256,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True)
class InstalledWheel:
    """One wheel as the executor verified and installed it."""

    service: str
    dist: str
    version: str
    file_sha256: str
    installed_sha256: str
    matches_recorded: bool

    def as_document(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "dist": self.dist,
            "version": self.version,
            "file_sha256": self.file_sha256,
            "installed_sha256": self.installed_sha256,
            "matches_recorded": self.matches_recorded,
        }


@dataclass(frozen=True)
class ExecutorReceipt:
    """The launch/install/command evidence every result carries."""

    python: str = ""
    workspace: str = ""
    venv_tool: str = ""
    installed: tuple[InstalledWheel, ...] = ()
    commands: tuple[CommandRun, ...] = ()

    def as_document(self) -> dict[str, Any]:
        return {
            "schema": EXECUTOR_RECEIPT_SCHEMA,
            "python": self.python,
            "workspace": self.workspace,
            "venv_tool": self.venv_tool,
            "installed": [wheel.as_document() for wheel in self.installed],
            "commands": [command.as_document() for command in self.commands],
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ExecutorReceipt:
        return cls(
            python=str(document.get("python", "")),
            workspace=str(document.get("workspace", "")),
            venv_tool=str(document.get("venv_tool", "")),
            installed=tuple(
                InstalledWheel(
                    service=str(wheel["service"]),
                    dist=str(wheel.get("dist", "")),
                    version=str(wheel.get("version", "")),
                    file_sha256=str(wheel.get("file_sha256", "")),
                    installed_sha256=str(wheel.get("installed_sha256", "")),
                    matches_recorded=bool(wheel.get("matches_recorded", False)),
                )
                for wheel in document.get("installed", ())
            ),
            commands=tuple(
                CommandRun(
                    name=str(command["name"]),
                    argv=tuple(command.get("argv", ())),
                    exit_code=int(command.get("exit_code", -1)),
                    log_sha256=str(command.get("log_sha256", "")),
                    duration_ms=int(command.get("duration_ms", 0)),
                )
                for command in document.get("commands", ())
            ),
        )


# ---------------------------------------------------------------------------
# Evidence record serialization (the report is JSON on disk).
# ---------------------------------------------------------------------------


def _evidence_to_document(record: EvidenceRecord) -> dict[str, Any]:
    return {
        "evidence_id": record.evidence_id,
        "dependencies": [
            {
                "repository_id": dep.repository_id,
                "candidate_oid": dep.candidate_oid,
                "image_digest": dep.image_digest,
                "role": dep.role,
            }
            for dep in sorted(record.dependencies, key=lambda dep: dep.repository_id)
        ],
        "test_bundle_digest": record.test_bundle_digest,
        "environment_profile_digest": record.environment_profile_digest,
        "environment_pins": [list(pair) for pair in record.environment_pins],
        "policy_refs": sorted(record.policy_refs),
        "superseded": record.superseded,
        "superseded_reason": record.superseded_reason,
    }


def _evidence_from_document(document: Mapping[str, Any]) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=str(document["evidence_id"]),
        dependencies=frozenset(
            DependencyIdentity(
                repository_id=str(dep["repository_id"]),
                candidate_oid=str(dep["candidate_oid"]),
                image_digest=str(dep["image_digest"]),
                role=str(dep["role"]),
            )
            for dep in document.get("dependencies", ())
        ),
        test_bundle_digest=document.get("test_bundle_digest"),
        environment_profile_digest=document.get("environment_profile_digest"),
        environment_pins=tuple(
            (str(pair[0]), str(pair[1])) for pair in document.get("environment_pins", ())
        ),
        policy_refs=frozenset(str(ref) for ref in document.get("policy_refs", ())),
        superseded=bool(document.get("superseded", False)),
        superseded_reason=str(document.get("superseded_reason", "")),
    )


# ---------------------------------------------------------------------------
# The executor report.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorReport:
    """The complete executor verification record for ONE frozen world."""

    schema: str = EXECUTOR_REPORT_SCHEMA
    tested_world_digest: str = ""
    applicability_digest: str = ""
    isolation_violated: bool = False
    isolation_unproven: bool = False
    authority_receipt: AuthorityReceipt = field(default_factory=AuthorityReceipt)
    enforcement_profile: dict[str, Any] = field(default_factory=dict)
    enforcement_profile_digest: str = ""
    executor_receipt: ExecutorReceipt = field(default_factory=ExecutorReceipt)
    edge_results: tuple[EdgeResult, ...] = ()
    system_ready: bool = False
    failed_members: tuple[str, ...] = ()
    report_coverage: dict[str, Any] = field(default_factory=dict)
    redelivery_outcome: dict[str, Any] = field(default_factory=dict)
    dependency_coverage: dict[str, str] = field(default_factory=dict)
    evidence_records: tuple[EvidenceRecord, ...] = ()
    readiness: dict[str, Any] = field(default_factory=dict)

    @property
    def isolation(self) -> str:
        """The tri-state isolation verdict (never one boolean): mirrors
        the launched process's authority receipt."""
        return self.authority_receipt.isolation

    def to_document(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "tested_world_digest": self.tested_world_digest,
            "applicability_digest": self.applicability_digest,
            "isolation_violated": self.isolation_violated,
            "isolation_unproven": self.isolation_unproven,
            "isolation": self.isolation,
            "authority_receipt": self.authority_receipt.as_document(),
            "enforcement_profile": dict(self.enforcement_profile),
            "enforcement_profile_digest": self.enforcement_profile_digest,
            "executor_receipt": self.executor_receipt.as_document(),
            "system_ready": self.system_ready,
            "failed_members": list(self.failed_members),
            "edge_results": [result.as_document() for result in self.edge_results],
            "report_coverage": dict(self.report_coverage),
            "redelivery_outcome": dict(self.redelivery_outcome),
            "dependency_coverage": dict(self.dependency_coverage),
            "evidence": [_evidence_to_document(record) for record in self.evidence_records],
            "readiness": dict(self.readiness),
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ExecutorReport:
        return cls(
            schema=str(document.get("schema", EXECUTOR_REPORT_SCHEMA)),
            tested_world_digest=str(document.get("tested_world_digest", "")),
            applicability_digest=str(document.get("applicability_digest", "")),
            isolation_violated=bool(document.get("isolation_violated", False)),
            isolation_unproven=bool(document.get("isolation_unproven", False)),
            authority_receipt=AuthorityReceipt.from_document(document.get("authority_receipt", {})),
            enforcement_profile=dict(document.get("enforcement_profile", {})),
            enforcement_profile_digest=str(document.get("enforcement_profile_digest", "")),
            executor_receipt=ExecutorReceipt.from_document(document.get("executor_receipt", {})),
            edge_results=tuple(
                EdgeResult(
                    edge_id=str(result["edge_id"]),
                    kind=str(result["kind"]),
                    status=str(result["status"]),
                    repositories=tuple(result.get("repositories", ())),
                    failed_member=result.get("failed_member"),
                    detail=str(result.get("detail", "")),
                    checks=tuple(dict(check) for check in result.get("checks", ())),
                    evidence_id=str(result.get("evidence_id", "")),
                )
                for result in document.get("edge_results", ())
            ),
            system_ready=bool(document.get("system_ready", False)),
            failed_members=tuple(document.get("failed_members", ())),
            report_coverage=dict(document.get("report_coverage", {})),
            redelivery_outcome=dict(document.get("redelivery_outcome", {})),
            dependency_coverage=dict(document.get("dependency_coverage", {})),
            evidence_records=tuple(
                _evidence_from_document(record) for record in document.get("evidence", ())
            ),
            readiness=dict(document.get("readiness", {})),
        )

    def ledger(self) -> EvidenceLedger:
        """The ledger of the evidence this report recorded (the same
        :class:`EvidenceLedger` the reference twin and the two-writer
        machinery consume)."""
        if not self.evidence_records:
            return EvidenceLedger()
        return EvidenceLedger().record(*self.evidence_records)

    def failed_edge_ids(self) -> tuple[str, ...]:
        return tuple(result.edge_id for result in self.edge_results if result.status != "passed")


def executor_readiness(
    report: ExecutorReport,
    candidate_set: CandidateSet,
    *,
    merge_approval: bool = False,
    deploy_approval: bool = False,
) -> SystemReadiness:
    """The readiness QUERY over an executor report — a pure lookup,
    with the two permission booleans recording EXPLICIT human grants
    (a green verification authorizes neither merge nor deploy)."""
    failed = report.failed_edge_ids()
    if report.isolation_violated or report.isolation_unproven:
        # a violated OR unproven isolation is a security prerequisite
        # failure: it BLOCKS verification — every edge — and is never
        # misclassified as a code defect on some member.
        failed = tuple(sorted({*failed, *(edge.edge_id for edge in executor_edges())}))
    return system_readiness(
        report.ledger(),
        candidate_set,
        executor_edges(),
        merge_approval=merge_approval,
        deploy_approval=deploy_approval,
        failed_edge_ids=failed,
    )


# ---------------------------------------------------------------------------
# The parent side: launch the subprocess under the scrubbed env.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutorRun:
    """One executor launch: what the launcher passed, what came back."""

    argv: tuple[str, ...]
    allowlist: tuple[str, ...]
    extra_env_keys: tuple[str, ...]
    launch_env_keys: tuple[str, ...]
    exit_code: int | None
    timed_out: bool
    stdout_sha256: str
    stderr_sha256: str
    report: ExecutorReport | None
    enforcement_profile_digest: str = ""

    @property
    def clean(self) -> bool:
        return self.exit_code == EXIT_CLEAN and self.report is not None


class IsolationViolated(RuntimeError):
    """A probe observed credential material where none should exist —
    the verification refuses to run (fail closed)."""


class IsolationUnproven(RuntimeError):
    """A probe prerequisite (positive control, clean HOME, PATH scope)
    could not be demonstrated — the isolation claim is ``unproven``
    and the verification is BLOCKED (never consumed as a pass)."""


def require_isolated(run: ExecutorRun) -> ExecutorRun:
    """Fail closed in-process: a run whose launched process observed
    credential material raises instead of being consumed as evidence,
    and a run whose isolation could not be DEMONSTRATED (unavailable
    or inconclusive probes, dirty HOME/PATH) is refused just as
    firmly — an unproven receipt is never evidence."""
    if run.report is None:
        raise IsolationViolated(
            "the executor produced no report"
            + (" (it timed out)" if run.timed_out else f" (exit {run.exit_code})")
        )
    verdict = run.report.authority_receipt.isolation
    if verdict == ISOLATION_VIOLATED or run.report.isolation_violated:
        violations = "; ".join(run.report.authority_receipt.violations) or "deny probe violation"
        raise IsolationViolated(f"the launched verification process was not isolated: {violations}")
    if verdict != ISOLATION_PROVEN or run.report.isolation_unproven:
        blockers = "; ".join(run.report.authority_receipt.blockers) or "isolation prerequisites"
        raise IsolationUnproven(
            "the launched verification process's isolation is unproven"
            f" ({run.report.authority_receipt.environment_hygiene} hygiene,"
            f" {run.report.authority_receipt.credential_non_disclosure} non-disclosure,"
            f" {run.report.authority_receipt.network_enforcement} enforcement): {blockers}"
        )
    return run


class VerificationExecutor:
    """Launch the verification as a REAL subprocess under a scrubbed
    environment (the trusted executor lane).

    The subprocess is the shipped module itself
    (``python -m forge.adaptive.verification_executor``); its env is
    the parent's intersected with *allowlist* plus *extra_env* (both
    recorded in the launch receipt), with TWO R38-07 enforcement
    overrides: the child's HOME is an ISOLATED EMPTY home the launcher
    provisions (the parent home — and any credential file planted
    under it — never reaches the verifier) and the child's PATH is
    reduced to the system minimum plus the resolved venv tooling. The
    whole configuration is recorded as the :class:`EnforcementProfile`
    (written to disk, passed as ``--enforcement-profile``, digested in
    the report as ``verification.enforcement_profile_digest``) and the
    launched process VALIDATES the declaration against what it
    observes. The controlled probes INSIDE the subprocess remain the
    behavioral backstop: an ungated endpoint or a misconfigured
    allowlist is caught from the launched process and fails the whole
    run closed.
    """

    def __init__(
        self,
        *,
        python: str | None = None,
        allowlist: Sequence[str] = DEFAULT_ENV_ALLOWLIST,
        extra_env: Mapping[str, str] | None = None,
        timeout: float = 300.0,
    ) -> None:
        self._python = python or sys.executable
        self._allowlist = tuple(sorted(set(allowlist)))
        self._extra_env = dict(extra_env or {})
        self._timeout = timeout

    def _tool_dirs(self) -> tuple[str, ...]:
        """The resolved venv tooling directories the narrowed PATH
        keeps (the executor's children dial ``uv`` by name)."""
        uv = shutil.which("uv")
        return (str(Path(uv).parent),) if uv else ()

    def _provision_clean_home(self, home: Path) -> Path:
        """An ISOLATED EMPTY home: recreated on every launch so only
        the allowlisted layout ever exists under it."""
        if home.exists():
            shutil.rmtree(home)
        home.mkdir(parents=True)
        return home

    def enforcement_profile(self, home: Path, path_entries: Sequence[str]) -> EnforcementProfile:
        """The isolation configuration this launcher uses — the scope
        every isolation claim in the report is bound to."""
        return EnforcementProfile(
            env_allowlist=self._allowlist,
            extra_env_keys=tuple(sorted(self._extra_env)),
            home_path=str(home),
            home_layout=(),
            path_entries=tuple(path_entries),
        )

    def build_argv(
        self,
        bundle_path: Path,
        out_path: Path,
        *,
        probes_only: bool = False,
        enforcement_profile_path: Path | None = None,
    ) -> tuple[str, ...]:
        argv = [
            self._python,
            "-m",
            "forge.adaptive.verification_executor",
            "--candidate-set",
            str(bundle_path),
            "--out",
            str(out_path),
        ]
        if enforcement_profile_path is not None:
            argv.extend(("--enforcement-profile", str(enforcement_profile_path)))
        if probes_only:
            argv.append("--probes-only")
        return tuple(argv)

    def run(
        self,
        bundle_path: Path,
        *,
        out_path: Path,
        probes_only: bool = False,
        home_dir: Path | None = None,
    ) -> ExecutorRun:
        """Launch and wait; the report file is the artifact of record."""
        home = self._provision_clean_home(
            home_dir if home_dir is not None else out_path.parent / "executor-home"
        )
        path_entries = narrowed_path_entries(extra_dirs=self._tool_dirs())
        profile = self.enforcement_profile(home, path_entries)
        profile_path = out_path.parent / "enforcement-profile.json"
        _write_json(profile_path, profile.as_document())
        argv = self.build_argv(
            bundle_path,
            out_path,
            probes_only=probes_only,
            enforcement_profile_path=profile_path,
        )
        env = scrubbed_environment(os.environ, self._allowlist, self._extra_env)
        env["HOME"] = str(home)  # the clean HOME override (R38-07)
        env["PATH"] = os.pathsep.join(path_entries)  # the narrowed PATH override
        out_path.parent.mkdir(parents=True, exist_ok=True)
        timed_out = False
        completed: subprocess.CompletedProcess[str] | None = None
        try:
            completed = subprocess.run(
                list(argv), env=env, capture_output=True, text=True, timeout=self._timeout
            )
        except subprocess.TimeoutExpired:
            timed_out = True
        report = (
            ExecutorReport.from_document(_read_json(out_path))
            if out_path.is_file() and not timed_out
            else None
        )
        return ExecutorRun(
            argv=argv,
            allowlist=self._allowlist,
            extra_env_keys=tuple(sorted(self._extra_env)),
            launch_env_keys=tuple(sorted(env)),
            exit_code=None if completed is None else completed.returncode,
            timed_out=timed_out,
            stdout_sha256=hashlib.sha256(
                (completed.stdout if completed else "").encode("utf-8")
            ).hexdigest(),
            stderr_sha256=hashlib.sha256(
                (completed.stderr if completed else "").encode("utf-8")
            ).hexdigest(),
            report=report,
            enforcement_profile_digest=profile.digest(),
        )


# ---------------------------------------------------------------------------
# The socket-based fake broker (a subprocess; duplicate delivery is
# injected AT THE SOCKET).
# ---------------------------------------------------------------------------


def _frame(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")


async def _serve_broker(
    bind: str,
    port: int,
    ready_file: Path,
    journal_path: Path,
    *,
    duplicates: int = DUPLICATE_DELIVERIES,
    idle_timeout: float = 120.0,
) -> int:
    """The fake broker: a tiny asyncio TCP server.

    Protocol (one JSON object per line, UTF-8):

    - a consumer opens a socket and sends ``{"op": "sub", "topic": …}``
      — the broker then delivers EVERY published message to it,
      *duplicates* times each (the duplicate injected at the socket);
    - the executor's control socket sends ``{"op": "pub", "messages":
      […]}`` and receives ``{"op": "published", "count": n}``;
    - the consumer answers each delivery with
      ``{"op": "ack", "id": …, "delivery_seq": k}``;
    - when every message has been delivered *duplicates* times AND
      acked for every delivery, the broker writes its journal, sends
      ``{"op": "end"}`` to every subscriber and exits 0.

    The journal is the BROKER-SIDE truth (deliveries sent, acks seen
    on the wire) the executor cross-checks against the consumer's own
    durable rows.
    """
    messages: list[dict[str, Any]] = []
    deliveries_sent: set[tuple[int, int, str, int]] = set()
    acks_seen: dict[tuple[str, int], int] = {}
    subscribers: dict[int, asyncio.StreamWriter] = {}
    next_subscriber = 0
    done = asyncio.Event()

    def _journal_document() -> dict[str, Any]:
        per_message: dict[str, dict[str, Any]] = {}
        for index, message in enumerate(messages):
            message_id = str(message.get("id", ""))
            sent = len({key for key in deliveries_sent if key[1] == index and key[2] == message_id})
            acked = sorted(seq for (mid, seq) in acks_seen if mid == message_id)
            per_message[message_id] = {"deliveries_sent": sent, "acks": acked}
        return {
            "schema": "forge.executor.fake-broker-journal/1",
            "messages": len(messages),
            "duplicates": duplicates,
            "per_message": per_message,
            "complete": bool(messages)
            and all(
                entry["deliveries_sent"] == duplicates and len(entry["acks"]) == duplicates
                for entry in per_message.values()
            ),
        }

    async def _drain(writer: asyncio.StreamWriter, subscriber: int) -> None:
        for index, message in enumerate(messages):
            message_id = str(message.get("id", ""))
            for seq in range(1, duplicates + 1):
                key = (subscriber, index, message_id, seq)
                if key in deliveries_sent:
                    continue
                deliveries_sent.add(key)
                writer.write(
                    _frame(
                        {
                            "op": "delivery",
                            "topic": ORDERS_TOPIC,
                            "delivery_seq": seq,
                            "message": message,
                        }
                    )
                )
            await writer.drain()

    def _maybe_complete() -> bool:
        if not messages or not subscribers:
            return False
        journal = _journal_document()
        if journal["complete"]:
            _write_json(journal_path, journal)
            for writer in subscribers.values():
                writer.write(_frame({"op": "end"}))
            done.set()
            return True
        return False

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal next_subscriber
        subscriber = -1
        try:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=idle_timeout)
                if not line:
                    break
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    break
                op = str(payload.get("op", ""))
                if op == "sub":
                    subscriber = next_subscriber
                    next_subscriber += 1
                    subscribers[subscriber] = writer
                    await _drain(writer, subscriber)
                elif op == "pub":
                    published = [dict(message) for message in payload.get("messages", [])]
                    messages.extend(published)
                    writer.write(_frame({"op": "published", "count": len(published)}))
                    await writer.drain()
                    for sub_id, sub_writer in list(subscribers.items()):
                        await _drain(sub_writer, sub_id)
                elif op == "ack":
                    delivery_seq = int(payload.get("delivery_seq", 0))
                    acks_seen[(str(payload.get("id", "")), delivery_seq)] = delivery_seq
                elif op == "shutdown":
                    break
                if _maybe_complete():
                    break
        except (TimeoutError, ConnectionError, OSError):
            pass  # the journal records whatever actually happened
        finally:
            if subscriber in subscribers:
                del subscribers[subscriber]
            try:
                writer.close()
                await writer.wait_closed()
            except (ConnectionError, OSError):  # pragma: no cover — client vanished
                pass

    server = await asyncio.start_server(_handle, bind, port)
    sockets = server.sockets or ()
    actual_port = sockets[0].getsockname()[1] if sockets else port
    _write_json(ready_file, {"port": int(actual_port), "bind": bind})
    try:
        await done.wait()
    finally:
        server.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=10.0)
        except TimeoutError:  # pragma: no cover — a wedged connection
            pass
    if not journal_path.is_file():  # pragma: no cover — shutdown before completion
        _write_json(journal_path, _journal_document())
    return 0


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------------------
# The in-subprocess verification driver.
# ---------------------------------------------------------------------------


def _run_command(
    commands: list[CommandRun], logs: Path, name: str, argv: Sequence[str]
) -> tuple[CommandRun, str]:
    """Run one command, capture its combined log, digest everything."""
    started = time.monotonic()
    completed = subprocess.run(list(argv), capture_output=True, text=True, timeout=180.0)
    duration = int((time.monotonic() - started) * 1000)
    log = (
        f"$ {' '.join(str(part) for part in argv)}\n"
        f"exit={completed.returncode}\n--- stdout ---\n{completed.stdout}"
        f"--- stderr ---\n{completed.stderr}"
    )
    log_path = logs / f"{name}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log)
    command = CommandRun(
        name=name,
        argv=tuple(str(part) for part in argv),
        exit_code=completed.returncode,
        log_sha256=hashlib.sha256(log.encode("utf-8")).hexdigest(),
        duration_ms=duration,
    )
    commands.append(command)
    return command, log


def _create_venv(commands: list[CommandRun], logs: Path, workspace: Path) -> tuple[Path, str]:
    """The executor's OWN venv (uv when available, stdlib otherwise)."""
    venv = workspace / "venv"
    uv = shutil.which("uv")
    if uv is not None:
        command, _log = _run_command(
            commands, logs, "venv-create", [uv, "venv", str(venv), "--python", sys.executable]
        )
        if command.exit_code == 0:
            return venv, "uv"
    command, _log = _run_command(
        commands, logs, "venv-create-stdlib", [sys.executable, "-m", "venv", str(venv)]
    )
    if command.exit_code != 0:
        raise RuntimeError(f"could not create the executor venv: exit {command.exit_code}")
    return venv, "stdlib-venv"


def _venv_python(venv: Path) -> str:
    candidate = venv / "bin" / "python"
    if not candidate.exists():  # pragma: no cover — Windows layouts
        candidate = venv / "Scripts" / "python.exe"
    return str(candidate)


def _install_wheel(
    commands: list[CommandRun],
    logs: Path,
    venv: Path,
    venv_tool: str,
    wheel: WheelRef,
) -> None:
    if venv_tool == "uv":
        uv = shutil.which("uv") or "uv"
        command, _log = _run_command(
            commands,
            logs,
            f"install-{wheel.service}",
            [uv, "pip", "install", "--python", _venv_python(venv), "--no-deps", str(wheel.path)],
        )
        if command.exit_code == 0:
            return
    command, _log = _run_command(
        commands,
        logs,
        f"install-{wheel.service}-stdlib",
        [
            _venv_python(venv),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            str(wheel.path),
        ],
    )
    if command.exit_code != 0:
        raise RuntimeError(
            f"installing the wheel for {wheel.service} failed: exit {command.exit_code}"
        )


def _inspect_installed(
    commands: list[CommandRun], logs: Path, venv: Path, dist: str
) -> tuple[str, str]:
    """Read the INSTALLED distribution's version and (when the
    installer recorded it) the direct-URL of the wheel it installed —
    the receipt digests those exact bytes back."""
    probe = (
        "import importlib.metadata, json, sys\n"
        "distribution = importlib.metadata.distribution(sys.argv[1])\n"
        "direct = distribution.read_text('direct_url.json')\n"
        "print(json.dumps({'version': distribution.version,"
        " 'direct_url': json.loads(direct) if direct else None}))\n"
    )
    command, log = _run_command(
        commands, logs, f"inspect-{dist}", [_venv_python(venv), "-c", probe, dist]
    )
    if command.exit_code != 0:
        raise RuntimeError(f"inspecting the installed {dist} failed:\n{log}")
    stdout = log.split("--- stdout ---\n", 1)[1].rsplit("--- stderr ---", 1)[0].strip()
    payload = json.loads(stdout)
    direct = payload.get("direct_url") or {}
    wheel_path = Path(str(direct.get("url", "")).removeprefix("file://"))
    digest = _sha256_file(wheel_path) if wheel_path.is_file() else ""
    return str(payload["version"]), digest


def _wait_for_file(path: Path, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.is_file():
            try:
                return _read_json(path)
            except json.JSONDecodeError:  # a half-written file — retry
                pass
        time.sleep(0.05)
    raise TimeoutError(f"{path} never became readable")


def _broker_arm(
    commands: list[CommandRun],
    workspace: Path,
    venv: Path,
    messages: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The REAL redelivery: dial a local fake-broker subprocess over
    TCP, publish the produced messages, let the INSTALLED consumer
    receive them (each delivered TWICE at the socket), and assert
    idempotency from BOTH the broker's journal and the consumer's own
    durable rows. No blanket exactly-once claim: the outcome records
    what was actually observed."""
    ready_file = workspace / "broker-ready.json"
    journal_path = workspace / "broker-journal.json"
    consumer_report = workspace / "consumer-report.json"
    broker_argv = [
        sys.executable,
        "-m",
        "forge.adaptive.verification_executor",
        "--serve-broker",
        "--port",
        "0",
        "--ready-file",
        str(ready_file),
        "--journal",
        str(journal_path),
    ]
    broker = subprocess.Popen(  # noqa: SIM115 — joined in every path below
        broker_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    consumer_argv: list[str] = []
    consumer: subprocess.Popen[str] | None = None
    broker_exit: int | None = None
    consumer_exit: int | None = None
    port = 0
    try:
        ready = _wait_for_file(ready_file)
        port = int(ready["port"])
        consumer_argv = [
            _venv_python(venv),
            "-m",
            "orders_projection.consumer",
            "--db",
            str(workspace / "consumer.db"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--report",
            str(consumer_report),
        ]
        consumer = subprocess.Popen(  # noqa: SIM115 — joined in every path below
            consumer_argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True
        )
        # the control socket: publish the producer's messages over TCP.
        with socket.create_connection(("127.0.0.1", port), timeout=10.0) as control:
            control_file = control.makefile("rwb")
            control_file.write(_frame({"op": "pub", "messages": [dict(m) for m in messages]}))
            control_file.flush()
            answer = json.loads(control_file.readline())
            if answer.get("op") != "published":  # pragma: no cover — protocol drift
                raise RuntimeError(f"the broker refused the publish: {answer}")
        broker_exit = broker.wait(timeout=120.0)
        assert consumer is not None
        consumer_exit = consumer.wait(timeout=120.0)
    finally:
        for process in (consumer, broker):
            if process is not None and process.poll() is None:  # pragma: no cover
                process.kill()
                process.wait(timeout=10.0)
    commands.append(
        CommandRun(
            name="fake-broker",
            argv=tuple(broker_argv),
            exit_code=-1 if broker_exit is None else broker_exit,
            log_sha256=_sha256_file(journal_path) if journal_path.is_file() else "",
            duration_ms=0,
        )
    )
    commands.append(
        CommandRun(
            name="orders-projection-consumer",
            argv=tuple(consumer_argv),
            exit_code=-1 if consumer_exit is None else consumer_exit,
            log_sha256=_sha256_file(consumer_report) if consumer_report.is_file() else "",
            duration_ms=0,
        )
    )
    journal = _read_json(journal_path) if journal_path.is_file() else {}
    consumer_outcome = _read_json(consumer_report) if consumer_report.is_file() else {}
    per_message = consumer_outcome.get("per_message", {})
    broker_per_message = journal.get("per_message", {})
    effects = {str(key): int(value.get("effects", 0)) for key, value in per_message.items()}
    exactly_once_all = (
        bool(messages)
        and broker_exit == 0
        and consumer_exit == 0
        and journal.get("complete") is True
        and bool(effects)
        and len(effects) == len(messages)
        and all(count == 1 for count in effects.values())
    )
    deliveries_total = sum(
        int(entry.get("deliveries_sent", 0)) for entry in broker_per_message.values()
    )
    return {
        "schema": REDELIVERY_OUTCOME_SCHEMA,
        "coverage": REFERENCE_COVERAGE,
        "broker_socket": f"127.0.0.1:{port}",
        "duplicates_injected_at_socket": int(journal.get("duplicates", DUPLICATE_DELIVERIES)),
        "messages": len(messages),
        "deliveries": deliveries_total,
        "acks": sum(len(entry.get("acks", ())) for entry in broker_per_message.values()),
        "effects_per_message": effects,
        "broker_complete": journal.get("complete") is True,
        "consumer_exit": -1 if consumer_exit is None else consumer_exit,
        "broker_exit": -1 if broker_exit is None else broker_exit,
        "exactly_once_all": exactly_once_all,
        "detail": (
            f"{len(messages)} messages delivered"
            f" {deliveries_total // max(len(messages), 1)}x over real sockets (the"
            " duplicate injected by the broker); the installed consumer applied"
            " exactly one business effect per message"
            if exactly_once_all
            else "the redelivery did NOT observe exactly one business effect per"
            f" message (broker complete={journal.get('complete')}, broker exit="
            f"{broker_exit}, consumer exit={consumer_exit}, effects={effects})"
        ),
    }


def _verify_bundle_documents(bundle: CandidateBundle) -> None:
    """The bundle's declared documents must be the FROZEN world's —
    recomputing each digest and comparing against the persisted
    identity (a bundle that lies about the world refuses)."""
    members = {member.repository_id: member for member in bundle.candidate_set.members}
    wheels = {wheel.service: wheel for wheel in bundle.wheels}
    for service, wheel in sorted(wheels.items()):
        member = members.get(service)
        if member is None or member.image_digest.removeprefix("sha256:") != wheel.sha256:
            raise ValueError(
                f"wheel digest for {service} ({wheel.sha256[:12]}…)"
                " does not match the frozen member identity"
                f" ({member.image_digest if member else '<absent>'})"
                " — verify THIS world's artifacts"
            )
    if _canonical_digest(bundle.contract_document) != bundle.candidate_set.contract_bundle_digest:
        raise ValueError("the bundle's contract document is not the frozen contract bundle")
    if _canonical_digest(bundle.test_document) != bundle.candidate_set.test_bundle_digest:
        raise ValueError("the bundle's test document is not the frozen test bundle")
    if (
        _canonical_digest(bundle.environment_document)
        != bundle.candidate_set.environment_profile_digest
    ):
        raise ValueError("the bundle's environment document is not the frozen profile")


def _blocked_report(
    bundle: CandidateBundle,
    authority: AuthorityReceipt,
    edges: tuple[SystemEdge, ...],
    profile_document: Mapping[str, Any] | None,
    *,
    isolation_violated: bool,
    isolation_unproven: bool,
) -> ExecutorReport:
    """The report for a run whose isolation gated everything: no edge
    ran, no evidence exists — only the authority receipt, the
    enforcement profile the claims would have been scoped to, and the
    reference-coverage labels."""
    return ExecutorReport(
        tested_world_digest=bundle.candidate_set.tested_world_digest or "",
        applicability_digest=bundle.candidate_set.applicability_digest or "",
        isolation_violated=isolation_violated,
        isolation_unproven=isolation_unproven,
        authority_receipt=authority,
        enforcement_profile=dict(profile_document or {}),
        enforcement_profile_digest=(
            EnforcementProfile.digest_document(profile_document) if profile_document else ""
        ),
        report_coverage=_report_coverage(edges, ()),
        dependency_coverage=dict(bundle.environment_document.get("coverage_labels", {})),
    )


def _execute_candidate_verification(
    bundle: CandidateBundle, out_path: Path, profile_document: Mapping[str, Any] | None = None
) -> int:
    """The subprocess driver: probes first (fail closed), then the
    built artifacts, then the edges, then the report. Any unexpected
    executor failure still writes a report — never a bare crash."""
    workspace = bundle.work_dir / "executor"
    workspace.mkdir(parents=True, exist_ok=True)
    logs = workspace / "logs"
    authority = introspect_and_probe(
        bundle.allowlist,
        bundle.probe_targets(),
        timeout=bundle.probe_timeout,
        declared_profile=profile_document,
    )
    edges = executor_edges()
    contract_edge, baseline_edge, environment_edge = edges
    if authority.isolation == ISOLATION_VIOLATED:
        report = _blocked_report(
            bundle,
            authority,
            edges,
            profile_document,
            isolation_violated=True,
            isolation_unproven=False,
        )
        _write_json(out_path, report.to_document())
        print("isolation violated: " + "; ".join(authority.violations), file=sys.stderr)
        return EXIT_ISOLATION_VIOLATED
    if authority.isolation == ISOLATION_UNPROVEN:
        # A prerequisite could not be demonstrated (dead control,
        # unavailable/inconclusive probes, missing HOME/PATH profile).
        # The verification is BLOCKED — never green, and never
        # misreported as a code defect on some member.
        report = _blocked_report(
            bundle,
            authority,
            edges,
            profile_document,
            isolation_violated=False,
            isolation_unproven=True,
        )
        _write_json(out_path, report.to_document())
        print(
            "isolation unproven: " + "; ".join(authority.blockers),
            file=sys.stderr,
        )
        return EXIT_ISOLATION_UNPROVEN

    commands: list[CommandRun] = []
    installed: list[InstalledWheel] = []
    checks_by_edge: dict[str, list[dict[str, Any]]] = {edge.edge_id: [] for edge in edges}
    redelivery: dict[str, Any] = {}
    produced: dict[str, Any] = {}
    upgrade: dict[str, Any] = {}
    projection: dict[str, Any] = {}
    producer_contract: dict[str, Any] = {}
    consumer_contract: dict[str, Any] = {}
    venv_tool = "not-created"
    execution_error = ""
    try:
        _verify_bundle_documents(bundle)
        verified: list[WheelRef] = []
        for wheel in sorted(bundle.wheels, key=lambda wheel: wheel.service):
            actual = _sha256_file(wheel.path)
            matches = actual == wheel.sha256
            installed.append(
                InstalledWheel(
                    service=wheel.service,
                    dist=wheel.service.replace("-", "_"),
                    version="",
                    file_sha256=actual,
                    installed_sha256="",
                    matches_recorded=matches,
                )
            )
            if not matches:
                reason = (
                    f"the wheel file's sha256 {actual[:12]}… is not the frozen digest"
                    f" {wheel.sha256[:12]}… — a rebuilt or replaced artifact"
                    " invalidates this proof; refreeze the world"
                )
                for edge in edges:
                    if wheel.service in edge.repositories:
                        checks_by_edge[edge.edge_id].append(
                            {
                                "check": "wheel-digest",
                                "service": wheel.service,
                                "status": "failed",
                                "detail": reason,
                            }
                        )
            else:
                verified.append(wheel)
        verified_services = {wheel.service for wheel in verified}
        python = ""
        if verified:
            venv, venv_tool = _create_venv(commands, logs, workspace)
            for index, wheel in enumerate(verified):
                slot = next(
                    i for i, entry in enumerate(installed) if entry.service == wheel.service
                )
                _install_wheel(commands, logs, venv, venv_tool, wheel)
                version, installed_digest = _inspect_installed(
                    commands, logs, venv, wheel.service.replace("-", "_")
                )
                installed[slot] = replace(
                    installed[slot], version=version, installed_sha256=installed_digest
                )
            python = _venv_python(venv)

        # 1. the wheels' OWN contract documents (the referee cross-check).
        if PRODUCER_SERVICE in verified_services:
            report_path = workspace / "producer-contract.json"
            _run_command(
                commands,
                logs,
                "producer-contract",
                [python, "-m", "orders_api.contract", "--report", str(report_path)],
            )
            producer_contract = _read_json(report_path) if report_path.is_file() else {}
        if CONSUMER_SERVICE in verified_services:
            report_path = workspace / "consumer-contract.json"
            _run_command(
                commands,
                logs,
                "consumer-contract",
                [python, "-m", "orders_projection.contract", "--report", str(report_path)],
            )
            consumer_contract = _read_json(report_path) if report_path.is_file() else {}

        # 2. the wheels' OWN tests (the unit pipelines, run in the venv).
        for service, module in (
            (PRODUCER_SERVICE, "orders_api.selftest"),
            (CONSUMER_SERVICE, "orders_projection.selftest"),
        ):
            if service not in verified_services:
                continue
            report_path = workspace / f"selftest-{service}.json"
            command, _log = _run_command(
                commands,
                logs,
                f"selftest-{service}",
                [python, "-m", module, "--report", str(report_path)],
            )
            results = _read_json(report_path) if report_path.is_file() else {}
            checks_by_edge[contract_edge.edge_id].append(
                {
                    "check": "wheel-selftest",
                    "service": service,
                    "status": "passed" if command.exit_code == 0 else "failed",
                    "exit_code": command.exit_code,
                    "tests": len(results.get("tests", ())),
                    "failed_tests": [
                        test for test in results.get("tests", ()) if test.get("status") != "passed"
                    ],
                    "log_sha256": command.log_sha256,
                }
            )

        # 3. the replay messages, produced THROUGH the installed producer.
        if PRODUCER_SERVICE in verified_services:
            report_path = workspace / "produced.json"
            _run_command(
                commands,
                logs,
                "produce",
                [
                    python,
                    "-m",
                    "orders_api.produce",
                    "--count",
                    str(bundle.scenario_messages),
                    "--report",
                    str(report_path),
                ],
            )
            produced = _read_json(report_path) if report_path.is_file() else {}

        # 4. the sqlite upgrade leg — the INSTALLED wheel's schema code.
        if CONSUMER_SERVICE in verified_services:
            report_path = workspace / "upgrade.json"
            _run_command(
                commands,
                logs,
                "schema-upgrade",
                [
                    python,
                    "-m",
                    "orders_projection.schema",
                    "--db",
                    str(workspace / "upgrade.db"),
                    "--seed",
                    str(bundle.seed_rows),
                    "--report",
                    str(report_path),
                ],
            )
            upgrade = _read_json(report_path) if report_path.is_file() else {}

        # 5. the baseline projection leg — the INSTALLED consumer's code.
        if CONSUMER_SERVICE in verified_services:
            report_path = workspace / "projection.json"
            _run_command(
                commands,
                logs,
                "baseline-projection",
                [
                    python,
                    "-m",
                    "orders_projection.project",
                    "--api",
                    bundle.baseline_api_served,
                    "--rows",
                    str(bundle.baseline_rows),
                    "--report",
                    str(report_path),
                ],
            )
            projection = _read_json(report_path) if report_path.is_file() else {}

        # 6. the socket-broker redelivery arm (real network round trips) —
        #    only when BOTH tested artifacts are the frozen ones.
        if verified_services >= {PRODUCER_SERVICE, CONSUMER_SERVICE}:
            redelivery = _broker_arm(commands, workspace, venv, produced.get("messages", ()))
    except Exception as error:  # noqa: BLE001 — the report is always written
        execution_error = f"{type(error).__name__}: {error}"
        print(f"executor failure: {execution_error}", file=sys.stderr)

    receipt = ExecutorReceipt(
        python=sys.executable,
        workspace=str(workspace),
        venv_tool=venv_tool,
        installed=tuple(installed),
        commands=tuple(commands),
    )

    # -- assemble the three edges -------------------------------------------
    results: list[EdgeResult] = []

    def _edge_result(
        edge: SystemEdge, status: str, failed_member: str | None, detail: str
    ) -> EdgeResult:
        return EdgeResult(
            edge_id=edge.edge_id,
            kind=edge.kind,
            status=status,
            repositories=edge.repositories,
            failed_member=failed_member,
            detail=detail,
            checks=tuple(checks_by_edge[edge.edge_id]),
            evidence_id=f"ex-{edge.edge_id}",
        )

    # contract edge: the referee dialect check, the wheels' own tests,
    # and the socket replay (one accepted business effect per message).
    referee_dialect = str(bundle.contract_document.get("dialect", ""))
    producer_dialect = str(producer_contract.get("dialect", ""))
    consumer_dialect = str(consumer_contract.get("dialect", ""))
    contract_failed: str | None = None
    contract_reasons: list[str] = []
    digest_checks = [
        check
        for check in checks_by_edge[contract_edge.edge_id]
        if check.get("check") == "wheel-digest"
    ]
    if digest_checks:
        first = sorted(digest_checks, key=lambda check: str(check["service"]))[0]
        contract_failed = str(first["service"])
        contract_reasons.append(str(first["detail"]))
    if producer_dialect and producer_dialect != referee_dialect:
        contract_failed = PRODUCER_SERVICE
        contract_reasons.append(
            f"the built producer speaks dialect {producer_dialect}; the frozen"
            f" contract bundle froze {referee_dialect}"
        )
    if consumer_dialect and consumer_dialect != referee_dialect:
        contract_failed = CONSUMER_SERVICE
        contract_reasons.append(
            f"the built consumer expects dialect {consumer_dialect}; the frozen"
            f" contract bundle froze {referee_dialect}"
        )
    replay_ok = bool(redelivery.get("exactly_once_all"))
    if redelivery and not replay_ok:
        contract_reasons.append(str(redelivery.get("detail", "the socket replay failed")))
        contract_failed = contract_failed or CONSUMER_SERVICE
    selftests_failed = sorted(
        {
            str(check["service"])
            for check in checks_by_edge[contract_edge.edge_id]
            if check.get("check") == "wheel-selftest" and check.get("status") != "passed"
        }
    )
    if selftests_failed:
        contract_failed = selftests_failed[0]
        contract_reasons.append(f"a built wheel's own tests failed: {selftests_failed}")
    checks_by_edge[contract_edge.edge_id].append(
        {
            "check": "contract-referee",
            "referee_dialect": referee_dialect,
            "producer_dialect": producer_dialect,
            "consumer_dialect": consumer_dialect,
            "status": "passed" if contract_failed is None else "failed",
        }
    )
    if redelivery:
        checks_by_edge[contract_edge.edge_id].append(
            {
                "check": "socket-replay",
                "messages": redelivery.get("messages", 0),
                "deliveries": redelivery.get("deliveries", 0),
                "status": "passed" if replay_ok else "failed",
            }
        )
    if contract_failed or contract_reasons or execution_error:
        member = contract_failed
        detail = "; ".join(contract_reasons or ["the contract edge did not run"])
        if execution_error:
            detail = f"{detail}; executor failure: {execution_error}"
        results.append(
            _edge_result(contract_edge, "failed", member, f"{detail} — failed member {member}")
        )
    else:
        results.append(
            _edge_result(
                contract_edge,
                "passed",
                None,
                f"built wheels' own tests green; dialect {referee_dialect} confirmed"
                f" from both installed wheels; {redelivery.get('messages', 0)} messages"
                f" replayed over real sockets with one business effect each",
            )
        )

    # baseline edge: the pinned baseline's served API + the consumer's
    # projection through the INSTALLED wheel's own code.
    api_ok = bundle.baseline_api_served == bundle.baseline_api_required
    projected = int(projection.get("projected", 0))
    rows = int(projection.get("rows", 0))
    projection_ok = api_ok and rows > 0 and projected == rows
    baseline_checks = checks_by_edge[baseline_edge.edge_id]
    baseline_checks.append(
        {
            "check": "baseline-projection",
            "served_at_pin": bundle.baseline_api_served,
            "required": bundle.baseline_api_required,
            "rows": rows,
            "projected": projected,
            "status": "passed" if projection_ok else "failed",
        }
    )
    if projection_ok:
        results.append(
            _edge_result(
                baseline_edge,
                "passed",
                None,
                f"{projected} rows served by {PINNED_SERVICE} at"
                f" {bundle.baseline_api_served} projected by the installed"
                f" {CONSUMER_SERVICE}",
            )
        )
    else:
        baseline_digest_checks = [
            check for check in baseline_checks if check.get("check") == "wheel-digest"
        ]
        if baseline_digest_checks:
            first = sorted(baseline_digest_checks, key=lambda check: str(check["service"]))[0]
            member = str(first["service"])
            detail = str(first["detail"])
        elif not api_ok:
            member = PINNED_SERVICE
            detail = (
                f"the pinned baseline serves {bundle.baseline_api_served} but the"
                f" consumer integrates with {bundle.baseline_api_required}"
            )
        elif execution_error:
            member = CONSUMER_SERVICE
            detail = f"the projection leg did not run: {execution_error}"
        else:
            member = CONSUMER_SERVICE
            detail = f"only {projected}/{rows} baseline rows projected by the installed consumer"
        results.append(
            _edge_result(baseline_edge, "failed", member, f"{detail} — failed member {member}")
        )

    # environment edge: the seeded upgrade through the installed wheel's
    # schema code + the socket redelivery idempotency.
    environment_checks = checks_by_edge[environment_edge.edge_id]
    upgrade_ok = bool(
        upgrade.get("preserved")
        and upgrade.get("schema_advanced")
        and upgrade.get("post_upgrade_write")
    )
    environment_checks.append(
        {
            "check": "db-upgrade",
            "status": "passed" if upgrade_ok else "failed",
            "coverage": REFERENCE_COVERAGE,
            **{
                key: upgrade.get(key)
                for key in (
                    "baseline_schema",
                    "target_schema",
                    "seed_rows",
                    "baseline_fingerprint",
                    "target_fingerprint",
                    "preserved",
                    "schema_advanced",
                    "post_upgrade_write",
                    "detail",
                )
            },
        }
    )
    if redelivery:
        environment_checks.append(
            {
                "check": "socket-redelivery",
                "status": "passed" if redelivery.get("exactly_once_all") else "failed",
                **{
                    key: redelivery.get(key)
                    for key in ("messages", "deliveries", "acks", "exactly_once_all", "detail")
                },
            }
        )
    environment_failed: str | None = None
    environment_reasons: list[str] = []
    environment_digest_checks = [
        check for check in environment_checks if check.get("check") == "wheel-digest"
    ]
    if environment_digest_checks:
        first = sorted(environment_digest_checks, key=lambda check: str(check["service"]))[0]
        environment_failed = str(first["service"])
        environment_reasons.append(str(first["detail"]))
    if not upgrade_ok:
        environment_failed = environment_failed or DB_DEPENDENCY
        environment_reasons.append(
            f"the installed wheel's schema upgrade failed:"
            f" {upgrade.get('detail', 'no outcome recorded')}"
        )
    if not redelivery:
        if not environment_digest_checks and not execution_error:
            environment_reasons.append("the socket redelivery arm did not run")
            environment_failed = environment_failed or BUS_DEPENDENCY
    elif not redelivery.get("exactly_once_all"):
        environment_failed = environment_failed or BUS_DEPENDENCY
        environment_reasons.append(str(redelivery.get("detail", "the redelivery failed")))
    if execution_error and not environment_reasons:
        environment_failed = environment_failed or DB_DEPENDENCY
        environment_reasons.append(f"executor failure: {execution_error}")
    if environment_failed or environment_reasons:
        results.append(
            _edge_result(
                environment_edge,
                "failed",
                environment_failed,
                "; ".join(environment_reasons) + f" — failed member {environment_failed}",
            )
        )
    else:
        results.append(
            _edge_result(
                environment_edge,
                "passed",
                None,
                f"upgrade {upgrade.get('baseline_schema')}->{upgrade.get('target_schema')}"
                f" preserved {upgrade.get('seed_rows')} seeded rows via the installed"
                f" wheel's schema code; {redelivery.get('messages', 0)} messages"
                " exactly-once over real-socket duplicate redelivery",
            )
        )

    failed_members = tuple(
        sorted({result.failed_member for result in results if result.failed_member})
    )
    records = tuple(
        record_evidence(result.evidence_id, bundle.candidate_set, covers=result.repositories)
        for result in results
        if result.status == "passed"
    )
    system_ready = not failed_members and all(result.status == "passed" for result in results)
    readiness = system_readiness(
        EvidenceLedger().record(*records) if records else EvidenceLedger(),
        bundle.candidate_set,
        edges,
        failed_edge_ids=tuple(result.edge_id for result in results if result.status != "passed"),
    ).to_document()
    report = ExecutorReport(
        tested_world_digest=bundle.candidate_set.tested_world_digest or "",
        applicability_digest=bundle.candidate_set.applicability_digest or "",
        authority_receipt=authority,
        enforcement_profile=dict(profile_document or {}),
        enforcement_profile_digest=(
            EnforcementProfile.digest_document(profile_document) if profile_document else ""
        ),
        executor_receipt=receipt,
        edge_results=tuple(results),
        system_ready=system_ready,
        failed_members=failed_members,
        report_coverage=_report_coverage(edges, results),
        redelivery_outcome=redelivery,
        dependency_coverage=dict(bundle.environment_document.get("coverage_labels", {})),
        evidence_records=records,
        readiness=readiness,
    )
    _write_json(out_path, report.to_document())
    return EXIT_CLEAN if system_ready else EXIT_VERIFICATION_FAILED


# ---------------------------------------------------------------------------
# The CLI: the executor subprocess and the fake-broker subprocess.
# ---------------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forge.adaptive.verification_executor",
        description=(
            "the trusted verification executor (R37-15): probes its own isolation,"
            " installs the candidate wheels by sha256, and drives the real"
            " dependency scenarios — or serves the socket-based fake broker"
        ),
    )
    parser.add_argument("--candidate-set", type=Path, help="the candidate bundle document")
    parser.add_argument("--out", type=Path, help="where the report JSON is written")
    parser.add_argument(
        "--enforcement-profile",
        type=Path,
        help=(
            "the launcher's enforcement profile document (clean HOME/PATH policy, env"
            " allowlist version, network policy class) — the scope the isolation claims"
            " are bound to; the launched process validates it against what it observes"
        ),
    )
    parser.add_argument(
        "--probes-only",
        action="store_true",
        help="run ONLY the isolation introspection + controlled probes (a preflight)",
    )
    parser.add_argument("--serve-broker", action="store_true", help="serve the fake broker")
    parser.add_argument("--port", type=int, default=0, help="broker port (0 = ephemeral)")
    parser.add_argument("--bind", default="127.0.0.1", help="broker bind address")
    parser.add_argument("--ready-file", type=Path, help="broker readiness file")
    parser.add_argument("--journal", type=Path, help="broker journal file")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.serve_broker:
        if args.ready_file is None or args.journal is None:
            print("--serve-broker needs --ready-file and --journal", file=sys.stderr)
            return EXIT_USAGE
        return asyncio.run(_serve_broker(args.bind, args.port, args.ready_file, args.journal))
    if args.candidate_set is None or args.out is None:
        print("--candidate-set and --out are required", file=sys.stderr)
        return EXIT_USAGE
    bundle = CandidateBundle.read(args.candidate_set)
    profile_document = (
        _read_json(args.enforcement_profile)
        if args.enforcement_profile is not None and args.enforcement_profile.is_file()
        else None
    )
    if args.probes_only:
        authority = introspect_and_probe(
            bundle.allowlist,
            bundle.probe_targets(),
            timeout=bundle.probe_timeout,
            declared_profile=profile_document,
        )
        report = _blocked_report(
            bundle,
            authority,
            executor_edges(),
            profile_document,
            isolation_violated=authority.isolation == ISOLATION_VIOLATED,
            isolation_unproven=authority.isolation == ISOLATION_UNPROVEN,
        )
        _write_json(args.out, report.to_document())
        if authority.isolation == ISOLATION_VIOLATED:
            return EXIT_ISOLATION_VIOLATED
        if authority.isolation == ISOLATION_UNPROVEN:
            return EXIT_ISOLATION_UNPROVEN
        return EXIT_CLEAN
    return _execute_candidate_verification(bundle, args.out, profile_document)


if __name__ == "__main__":
    raise SystemExit(main())
