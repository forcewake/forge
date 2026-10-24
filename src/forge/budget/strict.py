"""The native adapter trusts one reviewed callback bundle and explicit pricing bytes."""

from dataclasses import dataclass
from decimal import Decimal, localcontext
import hashlib
import json
import re

LITELLM_VERSION = "1.106.2"
# Updated with the packaged callback, and checked against its actual source files.
GUARD_DIGEST = "add330134ffe47f2a3e54a664de9ab6093ae873fb44bf8161ece2ba213014135"
ALLOWED_ROUTES = ("/chat/completions", "/v1/chat/completions", "/messages", "/v1/messages")


@dataclass(frozen=True)
class TrustedPolicy:
    digest: str
    models: tuple[str, ...]
    minimums: tuple[tuple[str, Decimal], ...]

    @classmethod
    def parse(cls, raw: bytes) -> "TrustedPolicy":
        def unique(items: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate pricing policy key")
                result[key] = value
            return result

        if len(raw) > 65536:
            raise ValueError("pricing policy exceeds bound")
        body = json.loads(raw, object_pairs_hook=unique)
        if (
            not isinstance(body, dict)
            or set(body) != {"schema", "litellm_version", "models"}
            or body["schema"] != "forge.strict-budget/1"
            or body["litellm_version"] != LITELLM_VERSION
            or not isinstance(body["models"], dict)
            or not body["models"]
        ):
            raise ValueError("unsupported strict pricing policy")
        for name, tariff in body["models"].items():
            if (
                not name
                or not isinstance(tariff, dict)
                or set(tariff)
                != {
                    "native_model",
                    "input_rate",
                    "output_rate",
                    "max_input_tokens",
                    "max_output_tokens",
                }
            ):
                raise ValueError("explicit complete model tariff is required")
            for axis in ("input_rate", "output_rate"):
                value = tariff[axis]
                if (
                    not isinstance(value, str)
                    or not Decimal(value).is_finite()
                    or Decimal(value) <= 0
                ):
                    raise ValueError("positive exact token price is required")
            for axis in ("max_input_tokens", "max_output_tokens"):
                if type(tariff[axis]) is not int or not 0 < tariff[axis] <= 2097152:
                    raise ValueError("physical model context ceiling is required")
        with localcontext() as context:
            context.prec = 80
            minimums = tuple(
                (
                    name,
                    Decimal(tariff["input_rate"]) * tariff["max_input_tokens"]
                    + Decimal(tariff["output_rate"]) * tariff["max_output_tokens"],
                )
                for name, tariff in sorted(body["models"].items())
            )
        return cls(hashlib.sha256(raw).hexdigest(), tuple(sorted(body["models"])), minimums)

    def minimum_usd(self, model: str) -> Decimal:
        for name, amount in self.minimums:
            if name == model:
                return amount
        raise ValueError("model is outside trusted pricing")

    def accepts(self, value: object, nonce: str, model: str, artifact_digest: str | None) -> bool:
        return isinstance(value, dict) and (
            value.get("schema") == "forge.strict-budget-readiness/1"
            and value.get("nonce") == nonce
            and value.get("guard_digest") == GUARD_DIGEST
            and value.get("policy_digest") == self.digest
            and value.get("litellm_version") == LITELLM_VERSION
            and value.get("models") == list(self.models)
            and model in self.models
            and value.get("routes") == list(ALLOWED_ROUTES)
            and value.get("durable_unknown_holds") is True
            and isinstance(artifact_digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", artifact_digest) is not None
            and value.get("artifact_digest") == artifact_digest
        )
