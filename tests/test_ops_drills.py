"""Runtime operating lessons remain; synthetic qualification scores retire."""

from forge.adaptive.admission import (
    AdmissionPolicy,
    NativeStatus,
    reconcile_draining,
    release_lease_with_evidence,
    record_native_start_intent,
    try_acquire_lease,
)
from tests.test_checkpoint_repository import _sqlite_factory


async def test_ambiguous_native_start_holds_capacity_until_terminal_probe():
    engine, factory = await _sqlite_factory()
    try:
        policy = AdmissionPolicy(max_active_per_project=1)
        lease = await try_acquire_lease(policy, 42, factory, run_id="run-a", provider="github")
        assert lease is not None
        assert await record_native_start_intent(factory, "run-a", "github:dispatch:nonce") == 1
        released = await release_lease_with_evidence(factory, "run-a", reason="cancelled locally")
        assert released.released == 0 and released.drained == 1
        assert (
            await try_acquire_lease(policy, 42, factory, run_id="run-b", provider="github") is None
        )

        async def unavailable(key):
            assert key == "github:dispatch:nonce"
            return NativeStatus.UNKNOWN

        assert await reconcile_draining(factory, unavailable) == 0
        assert (
            await try_acquire_lease(policy, 42, factory, run_id="run-b", provider="github") is None
        )

        async def terminal(key):
            return NativeStatus.TERMINAL

        assert await reconcile_draining(factory, terminal) == 1
        assert await reconcile_draining(factory, terminal) == 0
        assert (
            await try_acquire_lease(policy, 42, factory, run_id="run-b", provider="github")
            is not None
        )
    finally:
        await engine.dispose()


async def test_never_dispatched_bootstrap_refusal_returns_capacity_immediately():
    engine, factory = await _sqlite_factory()
    try:
        policy = AdmissionPolicy(max_active_per_project=1)
        await try_acquire_lease(policy, 42, factory, run_id="run-a")
        result = await release_lease_with_evidence(factory, "run-a", reason="preflight refused")
        assert result.released == 1 and result.drained == 0
        assert await try_acquire_lease(policy, 42, factory, run_id="run-b") is not None
    finally:
        await engine.dispose()
