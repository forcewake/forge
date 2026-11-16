"""Trusted text-model cost ceilings for the pinned native callback."""

from dataclasses import dataclass
from decimal import Decimal, localcontext, ROUND_CEILING, ROUND_DOWN
import hashlib
import json
from pathlib import Path

VERSION = "forge.strict-budget/1"
LITELLM_VERSION = "1.106.2"
ROUTES = frozenset({"completion", "acompletion", "anthropic_messages"})


class BudgetDenied(ValueError):
    pass


def money(value: object) -> Decimal:
    if not isinstance(value, (str, Decimal, int)) or isinstance(value, bool):
        raise BudgetDenied("cost must be exact decimal text")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise BudgetDenied("cost must be finite and nonnegative")
    sign, digits, exponent = parsed.as_tuple()
    if not isinstance(exponent, int) or max(-exponent, 0) > 18 or len(digits) + exponent > 20:
        raise BudgetDenied("cost exceeds exact storage bounds")
    return parsed


def wire_usd(value: Decimal) -> Decimal:
    """Round down to 1e-6 USD so a float-stored cap matches the approved allowance."""
    quantum = Decimal("0.000001")
    floored = value.quantize(quantum, rounding=ROUND_DOWN)
    snapped = Decimal(format(float(floored), "f")).quantize(quantum, rounding=ROUND_DOWN)
    if snapped < 0 or not snapped.is_finite():
        raise BudgetDenied("cost must be finite and nonnegative")
    return snapped


def observed_cost(value: object) -> Decimal:
    """Round a native floating observation upward to the durable money scale."""
    if isinstance(value, bool) or not isinstance(value, (str, Decimal, int, float)):
        raise BudgetDenied("native observation is not a numeric cost")
    parsed = Decimal(str(value))
    if not parsed.is_finite() or parsed < 0 or parsed >= Decimal("1e20"):
        raise BudgetDenied("native observation exceeds finite money bounds")
    with localcontext() as context:
        context.prec = 80
        return money(parsed.quantize(Decimal("1e-18"), rounding=ROUND_CEILING))


@dataclass(frozen=True)
class Tariff:
    native_model: str
    input_rate: Decimal
    output_rate: Decimal
    max_input_tokens: int
    max_output_tokens: int

    @classmethod
    def parse(cls, body: object) -> "Tariff":
        fields = {
            "native_model",
            "input_rate",
            "output_rate",
            "max_input_tokens",
            "max_output_tokens",
        }
        if not isinstance(body, dict) or set(body) != fields:
            raise BudgetDenied("unknown model tariff schema")
        if not isinstance(body["native_model"], str) or not body["native_model"]:
            raise BudgetDenied("native model identity is required")
        for field in ("max_input_tokens", "max_output_tokens"):
            if type(body[field]) is not int or not 0 < body[field] <= 2097152:
                raise BudgetDenied("model context limits must be positive and bounded")
        rates = money(body["input_rate"]), money(body["output_rate"])
        if min(rates) <= 0:
            raise BudgetDenied("unpriced model cannot receive a reservation")
        return cls(
            body["native_model"], *rates, body["max_input_tokens"], body["max_output_tokens"]
        )

    def quote(self, data: dict, call_type: str) -> Decimal:
        if call_type not in ROUTES:
            raise BudgetDenied("route is outside strict text-model coverage")
        output = data.get("max_tokens", data.get("max_completion_tokens"))
        if type(output) is not int or not 0 < output <= self.max_output_tokens:
            raise BudgetDenied("request must have an explicit bounded output limit")
        if data.get("n", 1) != 1 or data.get("best_of", 1) != 1:
            raise BudgetDenied("multiple generations are outside one reservation")
        feature = next(
            (
                name
                for name in (
                    "web_search_options",
                    "audio",
                    "modalities",
                    "input_audio",
                    "service_tier",
                    "speed",
                    "inference_geo",
                    "context_management",
                    "compaction",
                    "safeguards",
                    "advisor",
                    "per_turn_control",
                    "container",
                    "mcp_servers",
                    "betas",
                )
                if name in data
            ),
            None,
        )
        if feature is not None:
            raise BudgetDenied(f"unpriced feature: {feature}")
        _headers(data)
        if not isinstance(data.get("messages"), list) or "prompt" in data:
            raise BudgetDenied("strict model routes require explicit chat messages")
        inputs = {
            key: data.get(key)
            for key in ("messages", "system", "tools", "tool_choice", "response_format", "thinking")
        }
        if (
            len(json.dumps(inputs, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 512
            > self.max_input_tokens
        ):
            raise BudgetDenied("text context exceeds its conservative input-token bound")
        _text_only(data.get("messages"))
        _text_only(data.get("system"))
        tools = data.get("tools", [])
        if not isinstance(tools, list) or any(
            not isinstance(tool, dict) or tool.get("type") not in (None, "function", "custom")
            for tool in tools
        ):
            raise BudgetDenied("server-side paid tools are outside text pricing")
        # Reserve the full trusted input context, including cache-write surcharges
        # in input_rate, rather than trusting a caller's tokenizer estimate.
        with localcontext() as context:
            context.prec = 80
            return money(
                self.input_rate * self.max_input_tokens + self.output_rate * self.max_output_tokens
            )


def _text_only(value: object, depth: int = 0) -> None:
    if depth > 64:
        raise BudgetDenied("request nesting exceeds its bound")
    if isinstance(value, list):
        for child in value:
            _text_only(child, depth + 1)
    elif isinstance(value, dict):
        kind = value.get("type")
        if kind is not None and kind not in (
            "text",
            "tool_use",
            "tool_result",
            "thinking",
            "redacted_thinking",
        ):
            raise BudgetDenied("non-text input cannot use text-model pricing")
        for key, child in value.items():
            if key in ("image_url", "source", "audio", "file", "document"):
                raise BudgetDenied("non-text input cannot use text-model pricing")
            if key == "cache_control":
                if (
                    not isinstance(child, dict)
                    or set(child) - {"type", "ttl"}
                    or child.get("type") != "ephemeral"
                    or child.get("ttl", "5m") not in ("5m", "1h")
                ):
                    raise BudgetDenied("cache policy is outside trusted token pricing")
                continue
            # Tool JSON itself is inert input text, not another content envelope.
            if key not in ("input", "input_schema", "parameters"):
                _text_only(child, depth + 1)


def _headers(data: dict) -> None:
    # Native pass-through forwards caller headers. Their feature flags cannot
    # enable an unpriced fast route, a larger context or server-side tools.
    safe_betas = {
        "claude-code-20250219",
        "interleaved-thinking-2025-05-14",
        "fine-grained-tool-streaming-2025-05-14",
        "prompt-caching-2024-07-31",
    }
    headers = data.get("extra_headers", {})
    if not isinstance(headers, dict):
        raise BudgetDenied("provider headers must be explicit")
    for name, value in headers.items():
        if not isinstance(name, str) or not isinstance(value, str):
            raise BudgetDenied("provider header schema is unknown")
        if name.lower() == "anthropic-beta":
            if not set(value.split(",")) <= safe_betas:
                raise BudgetDenied("provider beta is outside trusted pricing")
        elif name.lower() not in ("anthropic-version", "x-client-app"):
            raise BudgetDenied("provider header is outside trusted text routing")


@dataclass(frozen=True)
class Policy:
    digest: str
    tariffs: dict[str, Tariff]

    @classmethod
    def parse(cls, raw: bytes) -> "Policy":
        def unique(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise BudgetDenied("duplicate policy key")
                result[key] = value
            return result

        if len(raw) > 65536:
            raise BudgetDenied("gateway policy exceeds its bound")
        body = json.loads(raw, object_pairs_hook=unique)
        if (
            not isinstance(body, dict)
            or set(body) != {"schema", "litellm_version", "models"}
            or body["schema"] != VERSION
            or body["litellm_version"] != LITELLM_VERSION
        ):
            raise BudgetDenied("unsupported gateway policy")
        if not isinstance(body["models"], dict) or not body["models"]:
            raise BudgetDenied("explicit model pricing is required")
        tariffs = {model: Tariff.parse(value) for model, value in body["models"].items()}
        return cls(hashlib.sha256(raw).hexdigest(), tariffs)


def code_digest() -> str:
    root = Path(__file__).parent
    joined = b""
    for name in (
        "guard_policy.py",
        "guard_ledger.py",
        "forge_budget_guard.py",
        "bootstrap.py",
        "build_manifest.py",
        "Dockerfile",
    ):
        data = (root / name).read_bytes()
        joined += name.encode("ascii") + b"\0" + str(len(data)).encode("ascii") + b"\0" + data
    return hashlib.sha256(joined).hexdigest()
