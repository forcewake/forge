"""Native proxy contract against the isolated, provider-free CI fixture only."""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import os
import secrets

import httpx


async def verify(origin: str) -> None:
    master = os.environ["FORGE_LITELLM_TEST_MASTER_KEY"]
    async with httpx.AsyncClient(base_url=origin, timeout=10, trust_env=False) as client:
        headers = {"Authorization": f"Bearer {master}"}
        for _ in range(60):
            try:
                response = await client.get("/health/liveliness")
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(1)
        else:
            raise RuntimeError("isolated LiteLLM fixture did not become ready")
        key = "sk-forge-ci-" + secrets.token_urlsafe(32)
        alias = "forge-ci-budget-" + secrets.token_hex(12)
        data = {
            "key": key,
            "key_alias": alias,
            "models": ["forge-budget-mock"],
            "max_budget": 1,
            "duration": "60s",
            "budget_duration": None,
            "key_type": "llm_api",
            "max_parallel_requests": 1,
            "metadata": {"fixture": "isolated-native-budget"},
        }
        response = await client.post("/key/generate", headers=headers, json=data)
        assert response.status_code == 200, "key creation refused"
        user = {"Authorization": f"Bearer {key}"}
        request = {
            "model": "forge-budget-mock",
            "messages": [{"role": "user", "content": "synthetic"}],
            "max_tokens": 16,
        }
        response = await client.post("/chat/completions", headers=user, json=request)
        assert response.status_code == 200, "configured native mock call failed"
        for _ in range(30):
            response = await client.get(
                "/key/list",
                headers=headers,
                params={"key_alias": alias, "return_full_object": "true", "size": 2},
            )
            assert response.status_code == 200, "alias read unavailable"
            body = response.json()
            assert body["total_count"] == 1 and len(body["keys"]) == 1, "alias not unique"
            row = body["keys"][0]
            assert row["token"] == hashlib.sha256(key.encode()).hexdigest(), "alias hash mismatch"
            assert row["models"] == ["forge-budget-mock"], "model scope widened"
            assert row["budget_duration"] is None, "native budget resets"
            assert datetime.fromisoformat(row["expires"]).astimezone(timezone.utc) > datetime.now(
                timezone.utc
            )
            if Decimal(str(row["spend"])) > Decimal("0.000000001"):
                break
            await asyncio.sleep(1)
        else:
            raise RuntimeError("native mock spend never became visible by alias")
        response = await client.post(
            "/key/update", headers=headers, json={"key": row["token"], "max_budget": 0.000000001}
        )
        assert response.status_code == 200, "native cap update refused"
        response = await client.post("/chat/completions", headers=user, json=request)
        assert response.status_code == 429, "native USD cap did not reject further requests"
        response = await client.post("/key/block", headers=headers, json={"key": row["token"]})
        assert response.status_code == 200 and response.json()["blocked"] is True, (
            "native block unconfirmed"
        )
        expiry_key = "sk-forge-ci-" + secrets.token_urlsafe(32)
        response = await client.post(
            "/key/generate",
            headers=headers,
            json={
                **data,
                "key": expiry_key,
                "key_alias": alias + "-expiry",
                "duration": "1s",
                "max_budget": 1,
            },
        )
        assert response.status_code == 200, "expiring key creation refused"
        await asyncio.sleep(2)
        response = await client.post(
            "/chat/completions", headers={"Authorization": f"Bearer {expiry_key}"}, json=request
        )
        assert response.status_code in (400, 401, 403), "expired key remained usable"
    print("LiteLLM native contract passed: USD rejection, expiry, hash-bound alias spend and block")


if __name__ == "__main__":
    asyncio.run(verify(os.environ["FORGE_LITELLM_TEST_URL"]))
