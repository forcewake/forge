"""Register the complete single-engine schema on its one declarative Base."""

from forge.store.inbox_models import EventInbox
from forge.store.models_budget import (
    EngineBudgetAmendment,
    EngineBudgetPool,
    EngineBudgetReservation,
    EngineGatewayKey,
    EngineUsage,
)
from forge.store.models_commands import EngineCommand
from forge.store.models_execution import EngineCandidate, EngineEffect, EngineStep
from forge.store.models_lane import (
    EngineBlob,
    EngineCheckpoint,
    EngineControlCommand,
    EngineLaneSession,
    EngineReviewRound,
)
from forge.store.models_runs import EngineApprovedInput, EngineGate, EngineRun, EngineRunSpec
from forge.store.schema.record import SchemaInstallation

__all__ = [
    "EventInbox",
    "EngineApprovedInput",
    "EngineBlob",
    "EngineBudgetAmendment",
    "EngineBudgetPool",
    "EngineBudgetReservation",
    "EngineCandidate",
    "EngineCheckpoint",
    "EngineControlCommand",
    "EngineCommand",
    "EngineEffect",
    "EngineGate",
    "EngineGatewayKey",
    "EngineLaneSession",
    "EngineReviewRound",
    "EngineRun",
    "EngineRunSpec",
    "EngineStep",
    "EngineUsage",
    "SchemaInstallation",
]
