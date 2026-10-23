"""Exercise the packaged native gateway against an isolated HTTP peer, never a provider."""

import asyncio
from decimal import Decimal
import hashlib
import os
from pathlib import Path
from uuid import uuid4

import httpx
from forge.budget.strict import TrustedPolicy

ROUTES = ["/chat/completions", "/v1/chat/completions", "/messages", "/v1/messages"]


class Proof:
    def __init__(self, client, upstream, admin, policy, artifact):
        self.client, self.upstream, self.admin = client, upstream, admin
        self.policy, self.artifact = policy, artifact

    async def readiness(self):
        nonce = uuid4().hex
        response = await self.client.get(
            "/forge/budget/readiness", headers=self.admin, params={"nonce": nonce}
        )
        assert response.status_code == 200
        body = response.json()
        assert self.policy.accepts(body, nonce, "forge-budget-stub", self.artifact)
        return body

    async def mint(self, usd="3", duration="300s"):
        ready = await self.readiness()
        key = "sk-forge-native-proof-" + uuid4().hex
        token = hashlib.sha256(key.encode()).hexdigest()
        body = {
            "key": key,
            "key_alias": "forge-proof-" + uuid4().hex,
            "models": ["forge-budget-stub"],
            "max_budget": float(usd),
            "duration": duration,
            "budget_duration": None,
            "key_type": "default",
            "allowed_routes": ROUTES,
            "max_parallel_requests": 1,
            "metadata": {
                "forge": {
                    "strict_policy_digest": ready["policy_digest"],
                    "allowance": {"usd": usd},
                }
            },
        }
        response = await self.client.post("/key/generate", headers=self.admin, json=body)
        assert response.status_code == 200, "native mint was refused"
        return key, token

    async def send(self, key, path="/v1/messages", **extra):
        return await self.client.post(
            path,
            headers={"Authorization": "Bearer " + key, "anthropic-version": "2023-06-01"},
            json={
                "model": "forge-budget-stub",
                "messages": [{"role": "user", "content": "synthetic only"}],
                "max_tokens": 32,
                **extra,
            },
        )

    async def block(self, token):
        response = await self.client.post("/key/block", headers=self.admin, json={"key": token})
        assert response.status_code == 200

    async def usage(self, token):
        response = await self.client.get(
            "/forge/budget/usage", headers=self.admin, params={"token": token}
        )
        assert response.status_code == 200
        return response.json()

    async def count(self):
        return (await self.upstream.get("/__stats")).json()["requests"]

    async def routes(self):
        for path in ("/v1/chat/completions", "/v1/messages"):
            for streaming in (False, True):
                key, token = await self.mint()
                response = await self.send(key, path, stream=streaming)
                assert response.status_code == 200
                if streaming:
                    assert ("[DONE]" if "chat" in path else "message_stop") in response.text
                # Native chat logging runs after the complete stream is assembled.
                await asyncio.sleep(0.15)
                await self.block(token)
                usage = await self.usage(token)
                assert usage["complete"] is True and usage["calls"] == 1 and usage["tokens"] == 14
                assert Decimal(usage["held_usd"]) == 0 and Decimal(usage["settled_usd"]) > 0
                print(f"PASS native {path} stream={streaming}: complete durable usage")

    async def refusal(self):
        key, token = await self.mint()
        count = await self.count()
        for fields in (
            {"max_tokens": None},
            {"api_base": "http://forbidden.invalid"},
            {"tools": [{"type": "web_search_20250305", "name": "search"}]},
            {"speed": "fast"},
            {"service_tier": "priority"},
            {"extra_headers": {"anthropic-beta": "fast-mode-2026-02-01"}},
            {"metadata": {"forge_budget_hold": {"request_id": "forged"}}},
            {"litellm_metadata": {"forge_budget_hold": {"request_id": "forged"}}},
        ):
            assert (await self.send(key, **fields)).status_code >= 400
        assert (await self.send(key, "/v1/embeddings")).status_code >= 400
        assert await self.count() == count
        await self.block(token)
        assert (await self.usage(token))["complete"] is True
        assert (await self.send(key)).status_code >= 400
        assert await self.count() == count
        print(
            "PASS native absent bound, deployment override, paid tool, other route and blocked key: no upstream call"
        )

    async def lost(self):
        key, token = await self.mint("0.9")
        before = await self.count()
        await self.upstream.post("/__control/drop-next", json={})
        assert (await self.send(key)).status_code >= 500
        assert await self.count() == before + 1, "native SDK retried an ambiguous paid request"
        assert (await self.send(key)).status_code == 429
        assert await self.count() == before + 1
        await self.block(token)
        usage = await self.usage(token)
        assert usage["complete"] is False and usage["calls"] is usage["tokens"] is None
        assert Decimal(usage["held_usd"]) == Decimal("0.72")
        print(
            "PASS native dropped response: one upstream dispatch, full durable hold, later admission refused"
        )
        return token

    async def expiry(self):
        key, token = await self.mint(duration="1s")
        await asyncio.sleep(1.1)
        before = await self.count()
        assert (await self.send(key)).status_code >= 400
        assert await self.count() == before
        await self.block(token)
        print("PASS native expired key: no upstream call")


async def main():
    url = os.environ["FORGE_LITELLM_TEST_URL"]
    upstream = os.environ["FORGE_LITELLM_TEST_UPSTREAM"]
    admin = {"Authorization": "Bearer " + os.environ["FORGE_LITELLM_TEST_MASTER_KEY"]}
    policy = TrustedPolicy.parse(
        Path("tests/fixtures/litellm/forge-budget-policy.json").read_bytes()
    )
    artifact = hashlib.sha256(
        Path(os.environ["FORGE_LITELLM_TEST_ARTIFACT"]).read_bytes()
    ).hexdigest()
    async with (
        httpx.AsyncClient(base_url=url, timeout=15, trust_env=False) as client,
        httpx.AsyncClient(base_url=upstream, timeout=15, trust_env=False) as peer,
    ):
        proof = Proof(client, peer, admin, policy, artifact)
        for _ in range(60):
            try:
                ready = await client.get(
                    "/forge/budget/readiness", headers=admin, params={"nonce": uuid4().hex}
                )
                if ready.status_code == 200:
                    await proof.readiness()
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        else:
            raise AssertionError("packaged native guard did not become ready")
        if saved := os.environ.get("FORGE_LITELLM_CHECK_RESTART"):
            usage = await proof.usage(Path(saved).read_text())
            assert usage["complete"] is False and Decimal(usage["held_usd"]) == Decimal("0.72")
            assert usage["calls"] is usage["tokens"] is None
            print("PASS native process restart: PostgreSQL retains the complete unknown hold")
            return
        await proof.routes()
        await proof.refusal()
        unknown_token = await proof.lost()
        await proof.expiry()
        # A nonsecret fingerprint lets CI repeat this observation after restart.
        if output := os.environ.get("FORGE_LITELLM_RESTART_PROOF"):
            Path(output).write_text(unknown_token)


if __name__ == "__main__":
    asyncio.run(main())
