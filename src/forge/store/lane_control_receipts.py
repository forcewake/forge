"""A native control call needs one durable intent; an unknown outcome cannot recall it."""

from datetime import datetime, timezone
from typing import Any

from forge_protocol import ControlAck, ControlCommand, ControlDelivery

from forge.api.lane_v1_values import LaneConflict
from forge.store.lane_auth import encoded
from forge.store.models_lane import EngineControlCommand
from forge.store.transactions import stored_time


def matching(command: ControlCommand, value: ControlDelivery) -> bool:
    return (
        command.forge_run_id,
        command.attempt,
        command.generation,
        command.command_id,
        command.sequence,
    ) == (
        value.forge_run_id,
        value.attempt,
        value.generation,
        value.command_id,
        value.sequence,
    )


def persist_receipts(
    row: EngineControlCommand, command: ControlCommand, state: dict[str, Any]
) -> None:
    raw = state.get("deliveries", {}).get(command.command_id)
    if raw is not None:
        delivery = ControlDelivery.model_validate(raw)
        document = encoded(delivery.model_dump(mode="json"))
        if not matching(command, delivery):
            raise LaneConflict("delivery identity differs from control")
        if row.delivery_json is None:
            if (
                row.state != "pending"
                or row.acknowledgement_json is not None
                or stored_time(row.deadline_at) <= datetime.now(timezone.utc)
            ):
                raise LaneConflict("control cannot start a native delivery")
            row.delivery_json, row.state = document, "inflight"
        elif row.delivery_json != document:
            raise LaneConflict("control delivery intent is immutable")
    raw_ack = state.get("acks", {}).get(command.command_id)
    if raw_ack is not None:
        ack = ControlAck.model_validate(raw_ack)
        if row.delivery_json is None or not matching(command, ack):
            raise LaneConflict("acknowledgment has no matching delivery intent")
        delivery = ControlDelivery.model_validate_json(row.delivery_json)
        if ack.delivery_id != delivery.delivery_id:
            raise LaneConflict("acknowledgment delivery identity differs")
        document = encoded(ack.model_dump(mode="json"))
        if row.acknowledgement_json not in (None, document):
            raise LaneConflict("control acknowledgment is already registered differently")
        if row.acknowledgement_json is None and row.state != "inflight":
            raise LaneConflict("control delivery is not inflight")
        row.acknowledgement_json = document
        row.state = "acknowledged" if ack.status == "applied" else "refused"
