"""Native LiteLLM callback: durable prospective holds before either text route."""

from decimal import Decimal, localcontext
from importlib.metadata import version
import json
import logging
from pathlib import Path
import re
from uuid import uuid4

from fastapi import Depends, HTTPException
from litellm.integrations.custom_logger import CustomLogger

from guard_ledger import NativeLedger
from guard_policy import (
    BudgetDenied,
    LITELLM_VERSION,
    Policy,
    code_digest,
    money,
    observed_cost,
    wire_usd,
)
from bootstrap import verify_artifact

POLICY_PATH = Path(__file__).with_name("forge-budget-policy.json")
ALLOWED_ROUTES = ["/chat/completions", "/v1/chat/completions", "/messages", "/v1/messages"]


def _native():
    from litellm.proxy import proxy_server

    return proxy_server


class ForgeBudgetGuard(CustomLogger):
    def __init__(self) -> None:
        super().__init__()
        self.policy = Policy.parse(POLICY_PATH.read_bytes())

    def _configuration(self) -> object:
        verify_artifact()
        native = _native()
        import litellm

        callbacks = list(getattr(litellm, "callbacks", []) or [])
        installed = self in callbacks or any(
            getattr(item, "__class__", type(item)).__name__ == "ForgeBudgetGuard"
            for item in callbacks
        )
        if not installed:
            raise BudgetDenied("native guard is not installed on the active callback path")
        if version("litellm") != LITELLM_VERSION or native.prisma_client is None:
            raise BudgetDenied("pinned gateway database is unavailable")
        settings = native.general_settings
        if (
            settings.get("fail_closed_budget_enforcement") is not True
            or settings.get("disable_budget_reservation") is True
        ):
            raise BudgetDenied("native prospective enforcement is not enabled")
        router = native.llm_router
        if (
            router is None
            or router.num_retries != 0
            or any(
                getattr(router, name, None)
                for name in ("fallbacks", "context_window_fallbacks", "content_policy_fallbacks")
            )
        ):
            raise BudgetDenied("hidden upstream retries or fallbacks are unavailable")
        return native

    def _model(self, native, model: str):
        import litellm

        tariff = self.policy.tariffs.get(model)
        deployments = [
            item for item in native.llm_router.model_list if item.get("model_name") == model
        ]
        if (
            tariff is None
            or len(deployments) != 1
            or deployments[0].get("litellm_params", {}).get("model") != tariff.native_model
        ):
            raise BudgetDenied("model does not have one trusted priced deployment")
        parameters = deployments[0]["litellm_params"]
        if parameters.get("max_retries") != 0 or parameters.get("num_retries") != 0:
            raise BudgetDenied("each native deployment must explicitly disable SDK retries")
        info = litellm.get_model_info(model=tariff.native_model)
        if any(
            type(info.get(name)) is not int or info[name] <= 0
            for name in ("max_input_tokens", "max_output_tokens")
        ):
            raise BudgetDenied("native model context ceiling is unknown")
        if (
            tariff.max_input_tokens < info["max_input_tokens"]
            or tariff.max_output_tokens < info["max_output_tokens"]
        ):
            raise BudgetDenied("trusted tariff must cover the model's complete physical context")
        for name, value in info.items():
            if "cost" not in name or not isinstance(value, (float, int)) or value <= 0:
                continue
            rate = (
                tariff.input_rate
                if "input" in name
                else tariff.output_rate
                if "output" in name
                else None
            )
            if "token" not in name or rate is None or Decimal(str(value)) > rate:
                raise BudgetDenied("native pricing is outside the trusted token-only ceiling")
        return tariff

    async def _ledger(self) -> NativeLedger:
        native = self._configuration()
        ledger = NativeLedger(native.prisma_client.db)
        await ledger.initialize()
        return ledger

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            native = self._configuration()
            model = data.get("model")
            tariff = self._model(native, model)
            if user_api_key_dict.allowed_routes != ALLOWED_ROUTES or user_api_key_dict.models != [
                model
            ]:
                raise BudgetDenied(
                    "key routes or models are not confined to approved text endpoints"
                )
            if any(
                name in data
                for name in (
                    "api_key",
                    "api_base",
                    "base_url",
                    "custom_llm_provider",
                    "fallbacks",
                    "router_settings_override",
                    "_router_weights",
                    "model_list",
                    "extra_body",
                    "mock_response",
                )
            ):
                raise BudgetDenied("request cannot override the trusted deployment")
            if any(data.get(name, 0) != 0 for name in ("num_retries", "max_retries")):
                raise BudgetDenied("caller cannot introduce hidden retries")
            data["num_retries"] = data["max_retries"] = 0
            reservation = user_api_key_dict.budget_reservation
            native_quote = Decimal(str((reservation or {}).get("reserved_cost", 0)))
            if (
                not isinstance(reservation, dict)
                or not native_quote.is_finite()
                or native_quote <= 0
                or not reservation.get("entries")
            ):
                raise BudgetDenied("native prospective price reservation is missing")
            token = user_api_key_dict.token
            if not isinstance(token, str):
                raise BudgetDenied("native key fingerprint is missing")
            forge = user_api_key_dict.metadata.get("forge")
            if (
                not isinstance(forge, dict)
                or forge.get("strict_policy_digest") != self.policy.digest
            ):
                raise BudgetDenied("key is not bound to the deployed strict pricing policy")
            cap = wire_usd(money(str(user_api_key_dict.max_budget)))
            approved = wire_usd(money(str(forge["allowance"]["usd"])))
            if cap > approved:
                raise BudgetDenied("native key ceiling exceeds the approved allowance")
            channels = []
            for name in ("metadata", "litellm_metadata"):
                channel = data.setdefault(name, {})
                if not isinstance(channel, dict) or "forge_budget_hold" in channel:
                    raise BudgetDenied("caller cannot supply native hold authority")
                channels.append(channel)
            request_id = uuid4().hex
            ledger = await self._ledger()
            await ledger.reserve(
                token, user_api_key_dict.key_alias, request_id, cap, tariff.quote(data, call_type)
            )
            # The opaque ID is not a credential; only this callback supplies it.
            for channel in channels:
                channel["forge_budget_hold"] = {"request_id": request_id, "token_digest": token}
            return data
        except Exception as error:
            hold = None
            for name in ("metadata", "litellm_metadata"):
                channel = data.get(name) if isinstance(data, dict) else None
                if isinstance(channel, dict) and isinstance(channel.get("forge_budget_hold"), dict):
                    hold = channel["forge_budget_hold"]
                    break
            if hold is not None:
                try:
                    await (await self._ledger()).release(hold["token_digest"], hold["request_id"])
                except Exception:
                    pass
            reason = str(error) if isinstance(error, BudgetDenied) else type(error).__name__
            logging.getLogger("forge.strict_budget").warning("Admission refused: %s", reason)
            raise HTTPException(status_code=429, detail="strict_budget_admission_refused") from None

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        # This event receives the complete assembled response for streaming too.
        # A missing callback, price or usage leaves its durable hold untouched.
        parameters = kwargs.get("litellm_params") or {}
        metadata = parameters.get("metadata") or kwargs.get("metadata") or {}
        hold = metadata.get("forge_budget_hold")
        cost = kwargs.get("response_cost")
        if not isinstance(hold, dict) or cost is None:
            return
        try:
            exact_cost = observed_cost(cost)
            if exact_cost <= 0:
                return
            usage = getattr(response_obj, "usage", None)
            tokens = (
                usage.get("total_tokens")
                if isinstance(usage, dict)
                else getattr(usage, "total_tokens", None)
            )
            if type(tokens) is not int or tokens <= 0:
                tokens = None
            ledger = await self._ledger()
            await ledger.settle(hold["token_digest"], hold["request_id"], exact_cost, 1, tokens)
        except Exception:
            # Logging is asynchronous. No refund is safer than claiming a final observation.
            return

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        # A 4xx that never left the gateway did not spend the provider. Release
        # that hold. Anything that may have been billed stays held.
        exception = kwargs.get("exception")
        status = getattr(exception, "status_code", None)
        if status not in (400, 401, 403, 404, 422):
            return
        parameters = kwargs.get("litellm_params") or {}
        metadata = parameters.get("metadata") or kwargs.get("metadata") or {}
        hold = metadata.get("forge_budget_hold")
        if not isinstance(hold, dict):
            return
        try:
            await (await self._ledger()).release(hold["token_digest"], hold["request_id"])
        except Exception:
            return

    async def _anthropic_usage(self, data, response):
        body = response if isinstance(response, dict) else response.model_dump(exclude_unset=True)
        usage = body.get("usage")
        fields = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        if not isinstance(usage, dict) or any(
            type(usage.get(name)) is not int or usage[name] < 0 for name in fields
        ):
            return
        hold = data.get("metadata", {}).get("forge_budget_hold")
        if not isinstance(hold, dict):
            return
        tariff = self._model(self._configuration(), data.get("model"))
        inputs = sum(usage[name] for name in fields if name != "output_tokens")
        with localcontext() as context:
            context.prec = 80
            cost = tariff.input_rate * inputs + tariff.output_rate * usage["output_tokens"]
        if cost > 0:
            ledger = await self._ledger()
            await ledger.settle(
                hold["token_digest"], hold["request_id"], cost, 1, inputs + usage["output_tokens"]
            )

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        try:
            await self._anthropic_usage(data, response)
        except Exception:
            pass
        return response

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data
    ):
        usage, pending, stopped = {}, "", False
        async for item in response:
            try:
                events = []
                if isinstance(item, dict):
                    events.append(item)
                elif isinstance(item, (str, bytes)):
                    pending += item.decode("utf-8") if isinstance(item, bytes) else item
                    if len(pending) > 1048576:
                        stopped = True
                        pending = ""
                    while "\n\n" in pending:
                        frame, pending = pending.split("\n\n", 1)
                        payload = "\n".join(
                            line[5:].lstrip()
                            for line in frame.splitlines()
                            if line.startswith("data:")
                        )
                        if payload and payload != "[DONE]":
                            events.append(json.loads(payload))
                for event in events:
                    kind = event.get("type")
                    if kind == "message_start":
                        usage.update(event.get("message", {}).get("usage", {}))
                    elif kind == "message_delta":
                        usage.update(event.get("usage", {}))
                    elif kind == "error":
                        stopped = True
                    elif kind == "message_stop" and not stopped:
                        await self._anthropic_usage(request_data, {"usage": usage})
            except Exception:
                stopped = True
            yield item

    @staticmethod
    def _admin(user_api_key_dict):
        role = str(getattr(user_api_key_dict, "user_role", ""))
        if role not in ("proxy_admin", "LitellmUserRoles.PROXY_ADMIN"):
            raise HTTPException(status_code=403, detail="admin_required")

    async def readiness(self, user_api_key_dict, nonce):
        self._admin(user_api_key_dict)
        if not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise HTTPException(status_code=422, detail="nonce_required")
        try:
            artifact = verify_artifact()
            native = self._configuration()
            ledger = await self._ledger()
            await ledger.db.query_raw(
                "SELECT token_digest FROM forge_budget_guard_accounts LIMIT 1"
            )
            for model in self.policy.tariffs:
                self._model(native, model)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="strict_budget_enforcement_unavailable: "
                + type(exc).__name__
                + ": "
                + str(exc)[:300],
            ) from None
        return {
            "schema": "forge.strict-budget-readiness/1",
            "nonce": nonce,
            "artifact_digest": artifact,
            "litellm_version": LITELLM_VERSION,
            "guard_digest": code_digest(),
            "policy_digest": self.policy.digest,
            "models": sorted(self.policy.tariffs),
            "routes": ALLOWED_ROUTES,
            "durable_unknown_holds": True,
        }

    async def usage(self, user_api_key_dict, token):
        self._admin(user_api_key_dict)
        if not re.fullmatch(r"[0-9a-f]{64}", token):
            raise HTTPException(status_code=422, detail="fingerprint_required")
        ledger = await self._ledger()
        rows = await ledger.db.query_raw(
            'SELECT blocked FROM "LiteLLM_VerificationToken" WHERE token=$1', token
        )
        if len(rows) != 1 or rows[0]["blocked"] is not True:
            raise HTTPException(status_code=409, detail="native_block_required")
        return {
            "schema": "forge.strict-budget-usage/1",
            "token_digest": token,
            "policy_digest": self.policy.digest,
            **await ledger.summary(token),
        }


guard = ForgeBudgetGuard()


# Registration is part of the loaded callback, so a missing hook also means no
# readiness endpoint. Its administrator dependency matches the native admin API.
async def readiness(nonce: str, user_api_key_dict=Depends(_native().user_api_key_auth)):
    return await guard.readiness(user_api_key_dict, nonce)


async def usage(token: str, user_api_key_dict=Depends(_native().user_api_key_auth)):
    return await guard.usage(user_api_key_dict, token)


_native().app.add_api_route("/forge/budget/readiness", readiness, methods=["GET"])
_native().app.add_api_route("/forge/budget/usage", usage, methods=["GET"])
