"""Explicit new-engine lane composition; no old-owner routing or fallback."""

from pathlib import Path

from fastapi import FastAPI

from forge.api.lane_auth import LaneAuthService
from forge.api.lane_v1_blobs import LaneBlobStore
from forge.api.lane_v1_service import LaneV1Service
from forge.store.lane_auth import EngineLaneAuthStore
from forge.store.lane_v1 import EngineLaneV1Store


def configure_engine_lane_v1(app: FastAPI) -> None:
    settings = app.state.settings
    app.state.lane_v1_service = None
    if not settings.FORGE_SECRET_KEY or not settings.FORGE_PUBLIC_URL:
        return
    auth = LaneAuthService(EngineLaneAuthStore(app.state.session_factory), settings)
    app.state.lane_v1_service = LaneV1Service(
        auth,
        EngineLaneV1Store(app.state.session_factory),
        LaneBlobStore(Path(settings.FORGE_LANE_BLOB_DIR)),
        model_key_provider=getattr(app.state, "lane_v1_model_key_provider", None),
    )
