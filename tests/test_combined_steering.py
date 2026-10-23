"""Steering is guidance; material contract changes need the revision gate."""

import pytest

from forge.adaptive.control import classify_instruction, deliver_steer, promote_to_proposal
from tests.test_adaptive_mailbox_db import _command


@pytest.mark.parametrize("text", ["fix the failing assertion first", "use the existing helper"])
def test_guidance_stays_inside_current_authority(text):
    command = _command(kind="steer", payload={"text": text})
    before = command.model_dump()
    delivered = deliver_steer(command, current_turn=7)
    assert delivered == {
        "command_id": command.command_id,
        "delivered_at_turn": 7,
        "status": "accepted",
    }
    assert command.model_dump() == before
    assert classify_instruction(text) == "steer"
    assert promote_to_proposal(text) is None


def test_new_architecture_constraint_requires_a_change_proposal():
    text = "do not introduce a new broker"
    assert classify_instruction(text) == "amend"
    assert promote_to_proposal(text) is not None


@pytest.mark.parametrize("text", ["skip the tests", "turn off checks", "make tests optional"])
def test_acceptance_weakening_cannot_be_smuggled_through_steering(text):
    result = deliver_steer(_command(kind="steer", payload={"text": text}), current_turn=3)
    assert result["status"] == "rejected"
    assert "revision gate" in result["reason"]
