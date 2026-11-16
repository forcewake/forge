"""Review is read-only, exact-input bound, bounded and inconclusive on uncertainty."""

import asyncio
from decimal import Decimal
import json

import pytest

from forge.domain.approved_input import ApprovedInput
from forge.domain.budget import Observation
from forge.port_values import ModelReply
from forge.review.reviewer import ReviewLimits, TextReviewer
from tests.test_domain_approved_input import spec

SHA = "b" * 40
DIFF = b"diff --git a/src/example.py b/src/example.py\n@@ -1 +1 @@\n-old\n+complete new tail\n"


class Model:
    def __init__(self, answer=None, failure=None):
        self.answer, self.failure, self.calls = answer, failure, []

    async def complete(self, **arguments):
        self.calls.append(arguments)
        if self.failure:
            raise self.failure
        prompt = json.loads(arguments["prompt"])
        answer = self.answer or json.dumps(
            {
                "candidate_sha": prompt["candidate_sha"],
                "approved_digest": prompt["approved_input"]["approved_digest"],
                "decision": "approve",
                "body": "The complete candidate satisfies the frozen request.",
            }
        )
        return ModelReply(
            text=answer,
            usage=Observation(usd=Decimal("0.01"), calls=1, tokens=100),
            evidence="synthetic model receipt",
        )


async def test_reviewer_receives_complete_frozen_spec_approval_diff_and_sha_without_write_tools():
    frozen, model = spec(), Model()
    approved = ApprovedInput.from_spec(frozen)
    verdict = await TextReviewer(frozen, model, credential_alias="closing-key").review(
        DIFF, approved, candidate_sha=SHA
    )
    assert verdict.decision == "approve" and verdict.candidate_sha == SHA
    assert len(model.calls) == 1
    call = model.calls[0]
    assert call["model"] == frozen.model and call["credential_alias"] == "closing-key"
    prompt = json.loads(call["prompt"])
    assert prompt["candidate_diff"].encode() == DIFF
    assert prompt["frozen_specification"] == frozen.model_dump(mode="json")
    assert prompt["approved_input"] == approved.model_dump(mode="json")
    assert "tools" not in call and "candidate_sha" in verdict.evidence
    checked = Model()
    await TextReviewer(frozen, checked, credential_alias="closing-key").review(
        DIFF, approved, candidate_sha=SHA, checks_passed=("unit", "lint")
    )
    recorded = json.loads(checked.calls[0]["prompt"])
    assert recorded["required_checks_passed"] == ["unit", "lint"]
    assert "still open" in checked.calls[0]["system"]
    assert "remain separate" not in checked.calls[0]["system"]


@pytest.mark.parametrize(
    "changes",
    [
        {"candidate_sha": "c" * 40},
        {"approved_digest": "c" * 64},
        {"decision": "unknown"},
        {"extra_authority": "merge"},
        {"body": ""},
    ],
)
async def test_review_response_cannot_change_its_sha_approval_or_closed_schema(changes):
    frozen = spec()
    approved = ApprovedInput.from_spec(frozen)
    model = Model(
        json.dumps(
            {
                "candidate_sha": SHA,
                "approved_digest": approved.approved_digest,
                "decision": "approve",
                "body": "complete",
                **changes,
            }
        )
    )
    result = await TextReviewer(frozen, model, credential_alias="closing").review(
        DIFF, approved, candidate_sha=SHA
    )
    assert result.decision == "inconclusive" and result.candidate_sha == SHA


@pytest.mark.parametrize(
    "answer",
    [
        "not JSON",
        '{"decision":"approve","decision":"approve"}',
        '{"decision":NaN}',
        "x" * 32769,
    ],
)
async def test_malformed_duplicate_nonfinite_or_oversized_review_never_becomes_approval(answer):
    frozen = spec()
    result = await TextReviewer(frozen, Model(answer), credential_alias="closing").review(
        DIFF, ApprovedInput.from_spec(frozen), candidate_sha=SHA
    )
    assert result.decision == "inconclusive"


@pytest.mark.parametrize(
    "diff,limits",
    [
        (b"bad\xff", ReviewLimits()),
        (b"raw\0binary", ReviewLimits()),
        (DIFF, ReviewLimits(diff_bytes=10)),
        (DIFF, ReviewLimits(prompt_bytes=10)),
    ],
)
async def test_unknown_or_over_limit_input_refuses_before_the_model_without_truncation(
    diff, limits
):
    frozen, model = spec(), Model()
    result = await TextReviewer(frozen, model, credential_alias="closing", limits=limits).review(
        diff, ApprovedInput.from_spec(frozen), candidate_sha=SHA
    )
    assert result.decision == "inconclusive" and model.calls == []


async def test_changed_valid_approval_refuses_before_model_and_model_errors_do_not_leak():
    frozen, model = spec(), Model()
    reviewer = TextReviewer(frozen, model, credential_alias="closing")
    changed = ApprovedInput.from_spec(spec(revision=2))
    assert (await reviewer.review(DIFF, changed, candidate_sha=SHA)).decision == "inconclusive"
    assert model.calls == []
    model.failure = RuntimeError("synthetic-private-response-details")
    result = await reviewer.review(DIFF, ApprovedInput.from_spec(frozen), candidate_sha=SHA)
    assert result.decision == "inconclusive"
    assert "synthetic-private" not in result.body + result.evidence


async def test_cancelled_review_preserves_cancellation_and_cannot_manufacture_a_verdict():
    started = asyncio.Event()

    class Waiting:
        async def complete(self, **kwargs):
            started.set()
            await asyncio.Future()

    frozen = spec()
    review = asyncio.create_task(
        TextReviewer(frozen, Waiting(), credential_alias="closing").review(
            DIFF, ApprovedInput.from_spec(frozen), candidate_sha=SHA
        )
    )
    await started.wait()
    review.cancel()
    with pytest.raises(asyncio.CancelledError):
        await review
