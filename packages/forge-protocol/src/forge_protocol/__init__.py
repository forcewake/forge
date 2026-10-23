"""Versioned wire contracts shared by the server and CI lane."""

from .canonical import approved_input_digest, canonical_json, digest
from .models import (
    PROTOCOL,
    Brief,
    CandidateBundle,
    CheckpointEntry,
    CheckpointManifest,
    ControlAck,
    ControlCommand,
    Heartbeat,
    LaneDispatch,
    LaneReport,
    ModelKeyGrant,
    RunIdentity,
    SessionRequest,
    SessionToken,
    Usage,
)

__version__ = "0.1.0"

__all__ = [
    "PROTOCOL",
    "Brief",
    "CandidateBundle",
    "CheckpointEntry",
    "CheckpointManifest",
    "ControlAck",
    "ControlCommand",
    "Heartbeat",
    "LaneDispatch",
    "LaneReport",
    "ModelKeyGrant",
    "RunIdentity",
    "SessionRequest",
    "SessionToken",
    "Usage",
    "approved_input_digest",
    "canonical_json",
    "digest",
]
