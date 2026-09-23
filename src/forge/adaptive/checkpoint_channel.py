"""The LIVE cross-runner checkpoint transport: lane runner -> control plane.

:mod:`forge.adaptive.checkpointing` made the pause transaction real, but
the manifest and its content blobs land in a store rooted on the LANE
JOB's filesystem — a second runner could not restore because nothing
carried the checkpoint off the dying runner. This module is that
transport (wave C/D):

1. **Upload** (:func:`upload_checkpoint`) — after a verified local
   capture, locate the work's LATEST ``forge.wip.manifest/2`` in the
   :class:`~forge.adaptive.artifact_store.ContentAddressedStore` (the
   manifest with the highest ``sequence`` whose ``work_id`` matches),
   re-fetch the manifest and every blob through the store's VERIFIED
   read, refuse any blob over the size cap BEFORE a single network
   byte leaves (an honest refusal, not a truncated upload), and PUT the
   whole checkpoint — manifest + blobs, JSON-base64 — to the control
   plane. The durable reference comes back only after the server's
   echoed checkpoint id is checked against the manifest's own content
   address: a server that claims a different checkpoint than the bytes
   describe is refused, never cited.
2. **Download** (:func:`download_checkpoint`) — on the SECOND runner,
   fetch the work's checkpoint into a LOCAL fresh store. Verification
   is client-side and total: the manifest bytes must hash to the
   advertised checkpoint id, the manifest must belong to the requested
   work, EVERY blob must reproduce its digest — a flipped byte is
   refused before anything is written, so a tampered transfer can never
   fabricate a restorable checkpoint — and only then are manifest and
   blobs put into the store and read back through
   :meth:`ContentAddressedStore.get_verified` (the store's own
   grant+digest semantics). The returned :class:`DownloadedCheckpoint`
   is the restore handle :func:`forge.adaptive.checkpointing.restore_wip`
   consumes. The lane passes it on with ``work_id=`` and
   ``promote="generation"`` (R32-01): the lane process sits INSIDE its
   target, so the restore lands a stable sibling
   ``.forge-workspace-gen-*`` workspace generation instead of replacing
   the checkout under the running process — see the checkpointing
   module's restore contract for the pointer-file and ownership
   (R32-02) semantics the downloaded handle inherits.

Authentication shares the lane control scheme's shared secret: the env
``FORGE_LANE_CONTROL_URL`` names the control plane and
``FORGE_LANE_CONTROL_SECRET`` is the shared secret BOTH sides derive
work-scoped bearer tokens from (:func:`work_scoped_token` — HMAC of the
work id under the secret, the same scheme the lane control token uses).
Every request carries ``Authorization: Bearer <token>``; the token for
the operator list surface is derived from the
:data:`CHECKPOINT_LIST_SCOPE` scope instead of a work id. When the
sibling lane-token module lands, the derivation should be unified
there — the wire contract (bearer HMAC under the shared secret) is the
stable part.

The channel is deliberately SYNCHRONOUS (``httpx.Client``): the capture
transaction it extends is synchronous, and the pause drain books its
failures the same way it books a failed capture — an upload refusal
raises, the pause lands ``paused_failed``, never a durable claim the
transport cannot support.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx

from forge.adaptive.artifact_store import ContentAddressedStore, CorruptArtifactError
from forge.adaptive.checkpointing import MANIFEST_SCHEMA

__all__ = [
    "CHECKPOINT_LIST_SCOPE",
    "DEFAULT_MAX_BLOB_BYTES",
    "FORGE_LANE_CONTROL_SECRET_ENV",
    "FORGE_LANE_CONTROL_URL_ENV",
    "CheckpointChannel",
    "CheckpointChannelError",
    "CheckpointRef",
    "CheckpointTooLarge",
    "DownloadedCheckpoint",
    "LaneControlAPI",
    "TamperedCheckpointError",
    "download_checkpoint",
    "format_checkpoint_ref",
    "parse_checkpoint_ref",
    "upload_checkpoint",
    "work_scoped_token",
]

#: The control plane's base URL (e.g. ``https://forge.example``). The
#: channel refuses to run without it — a guessed URL is not a transport.
FORGE_LANE_CONTROL_URL_ENV: Final = "FORGE_LANE_CONTROL_URL"

#: The SHARED SECRET the lane token scheme and this channel both derive
#: their work-scoped bearer tokens from (HMAC work-id under the secret).
#: The control plane answers 503 while it is unset — the channel is
#: disabled, fail closed.
FORGE_LANE_CONTROL_SECRET_ENV: Final = "FORGE_LANE_CONTROL_SECRET"

#: The scope string whose HMAC is the OPERATOR list token (the surface
#: ``GET /lane/checkpoints`` authenticates — it is not any one work's).
CHECKPOINT_LIST_SCOPE: Final = "checkpoints:list"

#: Largest single blob (manifest included) the channel will carry, in
#: either direction. Over the cap the transfer is REFUSED — honestly,
#: naming the blob, the size and the cap — instead of attempting a
#: truncated or streaming upload a small control plane would choke on.
#: The server enforces its own cap (413); this is the client's half.
DEFAULT_MAX_BLOB_BYTES: Final = 32 * 1024 * 1024

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class CheckpointChannelError(Exception):
    """A checkpoint-transport refusal (actionable, never a fabricated transfer).

    Names what the channel needed and why it could not proceed — no
    local checkpoint to upload, a control plane error, a malformed
    reference — so the pause drain books an honest ``paused_failed``
    rather than a durable reference nothing backs.
    """


class CheckpointTooLarge(CheckpointChannelError):
    """A blob exceeds the channel's size cap — refused before any transfer.

    The message carries the digest, the byte size and the cap: the fix
    is a larger cap (``max_blob_bytes`` / the server's env) or a smaller
    working tree, never a partially-uploaded checkpoint.
    """


class TamperedCheckpointError(CheckpointChannelError):
    """Downloaded bytes do not reproduce their advertised digests.

    A flipped blob byte, a manifest that hashes elsewhere than the
    advertised checkpoint id, or a checkpoint belonging to a different
    work. Raised BEFORE anything is written into the local store — the
    transfer is refused whole, never partially trusted.
    """


@dataclass(frozen=True)
class CheckpointRef:
    """The durable checkpoint reference the control plane hands back.

    ``checkpoint_id`` is the manifest's content address — identical on
    both sides by construction, which is what makes
    :attr:`remote_ref` (``<work_id>@<checkpoint_id>``) a reference a
    second runner can resolve with only the channel.
    """

    work_id: str
    checkpoint_id: str
    sequence: int = 0
    files: int = 0
    uploaded_at: str = ""

    @property
    def remote_ref(self) -> str:
        return format_checkpoint_ref(self.work_id, self.checkpoint_id)


@dataclass(frozen=True)
class DownloadedCheckpoint:
    """The restore handle :func:`download_checkpoint` returns.

    ``artifact_id`` is the manifest's content address in the LOCAL store
    — the very value a capture receipt would have carried on the first
    runner, which is the whole point: after a download, the second
    runner's restore path is indistinguishable from a local one.
    ``verified`` is True only after every blob AND the manifest were
    read back through the local store's verified read.
    """

    work_id: str
    artifact_id: str
    sequence: int = 0
    files: int = 0
    verified: bool = False


def work_scoped_token(secret: str, scope: str, *, generation: int | None = None) -> str:
    """The work-scoped bearer token: hex HMAC-SHA256(scope) under *secret*.

    Mirrors the lane control token scheme (HMAC of the work id under the
    shared ``FORGE_LANE_CONTROL_SECRET``): a runner may only touch the
    checkpoints of works it holds the secret for, and the operator list
    surface uses the :data:`CHECKPOINT_LIST_SCOPE` scope. Derived
    independently on both sides — the wire contract is just the bearer
    string.

    R28-07: with *generation* the token becomes ATTEMPT-SCOPED —
    ``HMAC(secret, scope + ":" + generation)`` — minted by the dispatch
    at dispatch time for the run's CURRENT generation, so a superseded
    runner generation's credential no longer validates once the work's
    generation moves. ``generation=None`` (the default, and the honest
    migration posture while dispatches still mint work-scoped tokens)
    derives exactly the legacy ``HMAC(secret, scope)`` bytes.
    """
    material = scope if generation is None else f"{scope}:{generation}"
    return hmac.new(secret.encode("utf-8"), material.encode("utf-8"), hashlib.sha256).hexdigest()


def format_checkpoint_ref(work_id: str, checkpoint_id: str) -> str:
    """``<work_id>@<checkpoint_id>`` — the portable durable reference."""
    return f"{work_id}@{checkpoint_id}"


def parse_checkpoint_ref(remote_ref: str) -> tuple[str, str]:
    """Split a durable reference into ``(work_id, checkpoint_id)``.

    Work ids never contain ``@`` (the server's safe-segment rule), so
    the split is total. A malformed reference — no ``@``, an empty
    work, a non-hex64 checkpoint id — raises :class:`ValueError`: the
    caller refuses rather than guessing which checkpoint was meant.
    """
    work_id, sep, checkpoint_id = remote_ref.partition("@")
    if not sep or not work_id or not _HEX64.fullmatch(checkpoint_id):
        raise ValueError(
            f"not a durable checkpoint reference ({remote_ref!r}): expected "
            "<work_id>@<64-hex checkpoint id>"
        )
    return work_id, checkpoint_id


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode(encoded: str) -> bytes:
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise CheckpointChannelError(f"control plane sent invalid base64 content: {exc}") from exc


def _is_hex64(value: object) -> bool:
    return isinstance(value, str) and bool(_HEX64.fullmatch(value))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _store_root(store: ContentAddressedStore) -> Path:
    """The store's on-disk root (package-internal access).

    The fan-out layout ``root/<first2>/<digest>`` is the store's
    documented on-disk shape; the channel lives in the same package and
    walks it read-only to LOCATE the work's latest manifest — the store
    exposes no enumeration surface because nothing outside the pause
    transaction ever needed one. Every artifact the walk selects is
    still fetched through the store's own verified read afterwards.
    """
    root = getattr(store, "_root", None)
    if not isinstance(root, Path):
        raise CheckpointChannelError(
            "the artifact store does not expose its content root — cannot locate "
            "the work's checkpoint manifest for upload"
        )
    return root


def _latest_manifest(store: ContentAddressedStore, work_id: str) -> tuple[str, dict]:
    """The work's LATEST ``forge.wip.manifest/2`` in the store.

    Every stored artifact is read, re-hashed to its own address (a
    rotted file is skipped, not trusted) and parsed; the candidates are
    the manifests whose ``work_id`` matches, and the winner is the
    highest-``sequence`` one (ties broken by address, deterministically)
    — the capture the pause most recently committed is the checkpoint a
    durable reference must point at.
    """
    candidates: list[tuple[int, str, dict]] = []
    for shard in sorted(_store_root(store).iterdir()):
        if not shard.is_dir():
            continue  # the persisted metadata document is a FILE at the root
        for artifact in sorted(shard.iterdir()):
            digest = artifact.name
            if not _HEX64.fullmatch(digest) or not artifact.is_file():
                continue  # quarantine siblings (.corrupt) are evidence, not checkpoints
            data = artifact.read_bytes()
            if _sha256(data) != digest:
                continue  # bytes no longer match their address — never select them
            try:
                document = json.loads(data)
            except ValueError:
                continue
            if (
                isinstance(document, dict)
                and document.get("schema") == MANIFEST_SCHEMA
                and document.get("work_id") == work_id
            ):
                candidates.append((int(document.get("sequence") or 0), digest, document))
    if not candidates:
        raise CheckpointChannelError(
            f"no checkpoint manifest for work {work_id!r} in the local store — "
            "capture_wip must run before the channel has anything durable to upload"
        )
    sequence, digest, document = max(candidates, key=lambda item: (item[0], item[1]))
    return digest, document


class LaneControlAPI:
    """The authenticated HTTP surface of the lane control plane.

    ``base_url`` defaults to :data:`FORGE_LANE_CONTROL_URL_ENV` and
    ``token`` to :data:`FORGE_LANE_CONTROL_SECRET_ENV` (the shared
    secret the work-scoped bearer tokens are derived from — see
    :func:`work_scoped_token`). An injected ``client`` (any
    ``httpx.Client`` — a FastAPI ``TestClient`` in tests) replaces the
    owned one and is never closed by this object. Every method refuses
    with :class:`CheckpointChannelError` naming the missing env when
    the configuration is absent: a channel that cannot reach the
    control plane says so, it does not guess.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        work_token: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self._base_url = (base_url or os.environ.get(FORGE_LANE_CONTROL_URL_ENV) or "").rstrip("/")
        # The lane holds ONLY its work-scoped token (never the shared
        # secret — EXE-04). When the token IS a pre-computed work token,
        # it rides as-is; when it's the secret (the control plane side),
        # per-request work tokens derive from it.
        self._secret = token or os.environ.get(FORGE_LANE_CONTROL_SECRET_ENV) or ""
        # The lane's PRE-COMPUTED work-scoped token (EXE-04: the shared
        # secret never enters a lane job; the dispatch provisions exactly
        # one HMAC). Rides as-is when set.
        self._direct_token = work_token
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def close(self) -> None:
        """Close the HTTP client when this object owns it."""
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> LaneControlAPI:
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    # -- request plumbing -----------------------------------------------------

    def _require_config(self) -> None:
        if not self._base_url:
            raise CheckpointChannelError(
                f"no control plane URL configured ({FORGE_LANE_CONTROL_URL_ENV}) — "
                "the checkpoint channel refuses to guess where to send a checkpoint"
            )
        if not self._secret and not self._direct_token:
            raise CheckpointChannelError(
                f"no lane control credential ({FORGE_LANE_CONTROL_SECRET_ENV} or a "
                "work-scoped token) — the checkpoint channel is disabled"
            )

    def _request(self, method: str, path: str, *, scope: str, payload: dict | None = None) -> dict:
        self._require_config()
        if self._direct_token:
            headers = {"Authorization": f"Bearer {self._direct_token}"}
        else:
            headers = {"Authorization": f"Bearer {work_scoped_token(self._secret, scope)}"}
        try:
            response = self._client.request(
                method, f"{self._base_url}{path}", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise CheckpointChannelError(f"control plane unreachable ({path}): {exc}") from exc
        if response.status_code >= 400:
            detail = ""
            try:
                detail = str(response.json().get("detail") or response.json().get("error") or "")
            except ValueError:
                detail = response.text[:200]
            raise CheckpointChannelError(
                f"control plane refused {method} {path}: "
                f"{response.status_code}{' — ' + detail if detail else ''}"
            )
        try:
            document = response.json()
        except ValueError as exc:
            raise CheckpointChannelError(
                f"control plane answered {method} {path} with non-JSON content"
            ) from exc
        if not isinstance(document, dict):
            raise CheckpointChannelError(
                f"control plane answered {method} {path} with a non-object JSON body"
            )
        return document

    # -- the three wire operations --------------------------------------------

    def put_checkpoint(self, work_id: str, payload: dict) -> dict:
        return self._request("PUT", f"/lane/checkpoints/{work_id}", scope=work_id, payload=payload)

    def get_checkpoint(self, work_id: str, *, checkpoint_id: str | None = None) -> dict:
        path = f"/lane/checkpoints/{work_id}"
        if checkpoint_id:
            path = f"{path}?checkpoint_id={checkpoint_id}"
        return self._request("GET", path, scope=work_id)

    def list_checkpoints(self) -> dict:
        return self._request("GET", "/lane/checkpoints", scope=CHECKPOINT_LIST_SCOPE)


class CheckpointChannel:
    """The bound transport: one control plane, one size cap, both directions.

    This is the object the checkpoint transaction wires in —
    ``capture_wip(upload=channel)`` and ``restore_wip(download=channel,
    artifact_id=<remote_ref>)`` — and the module-level
    :func:`upload_checkpoint` / :func:`download_checkpoint` functions
    are its plain-function spellings.
    """

    def __init__(
        self, api: LaneControlAPI | None = None, *, max_blob_bytes: int = DEFAULT_MAX_BLOB_BYTES
    ) -> None:
        self._api = api if api is not None else LaneControlAPI()
        self._max_blob_bytes = max_blob_bytes

    @property
    def api(self) -> LaneControlAPI:
        return self._api

    # -- upload ----------------------------------------------------------------

    def _cap_check(self, data: bytes, what: str) -> None:
        if len(data) > self._max_blob_bytes:
            raise CheckpointTooLarge(
                f"{what} is {len(data)} bytes; this channel carries at most "
                f"{self._max_blob_bytes} bytes per blob — the checkpoint transfer "
                "is refused rather than truncated"
            )

    def upload_checkpoint(self, store: ContentAddressedStore, work_id: str) -> CheckpointRef:
        """PUT the work's latest local checkpoint on the control plane.

        The manifest is located locally (highest ``sequence`` for the
        work), then the manifest and every blob are re-read through the
        store's VERIFIED read — an incomplete or rotting local
        checkpoint is refused, never uploaded — size-checked against
        the cap, and sent as one JSON-base64 document. The returned
        :class:`CheckpointRef` is minted only when the server's echoed
        checkpoint id equals the manifest's own content address.
        """
        manifest_id, manifest = _latest_manifest(store, work_id)
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise CheckpointChannelError(
                f"manifest {manifest_id} carries no files section — not a checkpoint"
            )
        try:
            manifest_bytes = store.get_verified(manifest_id, principal=store.tenant)
        except CorruptArtifactError as exc:
            raise CheckpointChannelError(f"the local manifest is corrupt: {exc}") from exc
        if manifest_bytes is None:
            raise CheckpointChannelError(
                f"manifest {manifest_id} does not resolve for tenant {store.tenant!r} — "
                "the local capture is incomplete, nothing durable can be uploaded"
            )
        self._cap_check(manifest_bytes, f"manifest {manifest_id}")

        blobs: dict[str, str] = {}
        for rel, entry in sorted(files.items()):
            digest = entry.get("digest") if isinstance(entry, dict) else None
            if not _is_hex64(digest):
                raise CheckpointChannelError(
                    f"manifest entry {rel!r} carries no valid content digest — "
                    "a bare filename uploads nothing"
                )
            try:
                data = store.get_verified(str(digest), principal=store.tenant)
            except CorruptArtifactError as exc:
                raise CheckpointChannelError(
                    f"local blob for {rel} ({digest}) is corrupt: {exc}"
                ) from exc
            if data is None:
                raise CheckpointChannelError(
                    f"required blob {digest} for {rel} is absent from the local store — "
                    "the checkpoint is incomplete, nothing durable can be uploaded"
                )
            self._cap_check(data, f"blob {digest} for {rel}")
            blobs[str(digest)] = _b64encode(data)

        sequence = int(manifest.get("sequence") or 0)
        response = self._api.put_checkpoint(
            work_id,
            {
                "manifest": _b64encode(manifest_bytes),
                "blobs": blobs,
                "sequence": sequence,
            },
        )
        checkpoint_id = response.get("checkpoint_id")
        if checkpoint_id != manifest_id:
            raise CheckpointChannelError(
                f"control plane echoed checkpoint id {checkpoint_id!r} for bytes whose "
                f"manifest addresses to {manifest_id} — the durable reference does not "
                "describe the uploaded checkpoint; refusing it"
            )
        return CheckpointRef(
            work_id=work_id,
            checkpoint_id=str(checkpoint_id),
            sequence=int(response.get("sequence") or sequence),
            files=int(response.get("files") or len(files)),
            uploaded_at=str(response.get("uploaded_at") or ""),
        )

    # -- download ----------------------------------------------------------------

    def download_checkpoint(
        self,
        work_id: str,
        store: ContentAddressedStore,
        *,
        checkpoint_id: str | None = None,
    ) -> DownloadedCheckpoint:
        """Fetch the work's checkpoint into a LOCAL fresh store, verified.

        Every promise is checked against the BYTES before anything is
        written: the manifest must hash to the advertised checkpoint
        id, belong to the requested work, carry the current schema, and
        every blob must reproduce its digest — one flipped byte refuses
        the whole transfer (:class:`TamperedCheckpointError`) leaving
        the local store untouched. Only after the full verification are
        manifest and blobs put into the store and read back through its
        verified read; the returned handle names the manifest's LOCAL
        content address.
        """
        response = self._api.get_checkpoint(work_id, checkpoint_id=checkpoint_id)
        checkpoint_id = str(response.get("checkpoint_id") or "")
        manifest_bytes = _b64decode(str(response.get("manifest") or ""))
        if checkpoint_id and _sha256(manifest_bytes) != checkpoint_id:
            raise TamperedCheckpointError(
                f"manifest bytes hash to {_sha256(manifest_bytes)} but the control "
                f"plane advertised checkpoint {checkpoint_id} — refused, nothing written"
            )
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError as exc:
            raise CheckpointChannelError(f"downloaded manifest is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
            raise CheckpointChannelError(
                "downloaded manifest is not a supported checkpoint schema — restore "
                "requires blobs, not a filename list"
            )
        if manifest.get("work_id") != work_id:
            raise TamperedCheckpointError(
                f"the control plane served a checkpoint belonging to work "
                f"{manifest.get('work_id')!r} for requested work {work_id!r} — refused"
            )
        files = manifest.get("files")
        if not isinstance(files, dict):
            raise CheckpointChannelError("downloaded manifest carries no files section")

        encoded_blobs = response.get("blobs")
        if not isinstance(encoded_blobs, dict):
            raise CheckpointChannelError("the checkpoint response carries no blobs section")
        blobs: dict[str, bytes] = {}
        for rel, entry in sorted(files.items()):
            digest = entry.get("digest") if isinstance(entry, dict) else None
            if not _is_hex64(digest):
                raise CheckpointChannelError(
                    f"downloaded manifest entry {rel!r} carries no valid content digest"
                )
            if str(digest) not in encoded_blobs:
                raise CheckpointChannelError(
                    f"the control plane is missing blob {digest} for {rel} — the "
                    "checkpoint it advertises cannot be reconstructed"
                )
            data = _b64decode(str(encoded_blobs[str(digest)]))
            self._cap_check(data, f"blob {digest} for {rel}")
            if _sha256(data) != digest:
                raise TamperedCheckpointError(
                    f"blob {digest} for {rel} failed digest verification "
                    f"(hashes to {_sha256(data)}) — refused, nothing written"
                )
            blobs[str(digest)] = data

        # Everything verified — NOW land it in the local store and prove the
        # landing with the store's own verified read (grant + digest).
        for digest, data in blobs.items():
            store.put(data)
        artifact_id = store.put(manifest_bytes, content_type="application/json")
        for digest in blobs:
            if store.get_verified(digest, principal=store.tenant) is None:
                raise CheckpointChannelError(
                    f"blob {digest} did not resolve digest-identical in the local "
                    "store after the download — the restore handle is refused"
                )
        if store.get_verified(artifact_id, principal=store.tenant) is None:
            raise CheckpointChannelError(
                "the downloaded manifest did not resolve digest-identical in the "
                "local store — the restore handle is refused"
            )
        return DownloadedCheckpoint(
            work_id=work_id,
            artifact_id=artifact_id,
            sequence=int(manifest.get("sequence") or 0),
            files=len(files),
            verified=True,
        )

    # -- the wiring seams ---------------------------------------------------------

    def fetch_checkpoint(self, remote_ref: str, store: ContentAddressedStore) -> str:
        """Resolve a durable reference into a LOCAL manifest address.

        The seam :func:`forge.adaptive.checkpointing.restore_wip`
        consumes through ``download=``: parse the reference, download
        digest-verified into *store*, and return the manifest's content
        address the local restore continues from. Callers restoring the
        lane's OWN working directory pass ``promote="generation"`` and
        their ``work_id`` alongside (R32-01/R32-02) so the promotion
        lands a stable sibling generation and only touches recovery
        assets this work owns.
        """
        work_id, checkpoint_id = parse_checkpoint_ref(remote_ref)
        handle = self.download_checkpoint(work_id, store, checkpoint_id=checkpoint_id)
        return handle.artifact_id

    def list_checkpoints(self) -> tuple[CheckpointRef, ...]:
        """The operator view: every checkpoint the control plane holds."""
        document = self._api.list_checkpoints()
        entries = document.get("checkpoints")
        if not isinstance(entries, list):
            raise CheckpointChannelError("the control plane's checkpoint list is malformed")
        refs: list[CheckpointRef] = []
        for entry in entries:
            if not isinstance(entry, dict) or not _is_hex64(entry.get("checkpoint_id")):
                raise CheckpointChannelError("the control plane's checkpoint list is malformed")
            refs.append(
                CheckpointRef(
                    work_id=str(entry.get("work_id") or ""),
                    checkpoint_id=str(entry["checkpoint_id"]),
                    sequence=int(entry.get("sequence") or 0),
                    files=int(entry.get("files") or 0),
                    uploaded_at=str(entry.get("uploaded_at") or ""),
                )
            )
        return tuple(refs)


def upload_checkpoint(
    store: ContentAddressedStore, work_id: str, api: LaneControlAPI
) -> CheckpointRef:
    """PUT the work's latest local checkpoint to the control plane (*api*).

    Returns the durable reference (see :meth:`CheckpointChannel.upload_checkpoint`).
    """
    return CheckpointChannel(api).upload_checkpoint(store, work_id)


def download_checkpoint(
    work_id: str,
    api: LaneControlAPI,
    store: ContentAddressedStore,
    *,
    checkpoint_id: str | None = None,
) -> DownloadedCheckpoint:
    """Fetch the work's checkpoint from the control plane into a LOCAL store.

    Without *checkpoint_id* the control plane serves the work's ACTIVE
    checkpoint (highest sequence); with it, EXACTLY that one — the
    resume leg's R28-05 contract: a ResumeSpec binds the exact approved
    checkpoint, and ``LaneControlAPI.get_checkpoint`` passes the id
    through as ``?checkpoint_id=``. Returns the verified restore handle
    (see :meth:`CheckpointChannel.download_checkpoint`).
    """
    return CheckpointChannel(api).download_checkpoint(work_id, store, checkpoint_id=checkpoint_id)
