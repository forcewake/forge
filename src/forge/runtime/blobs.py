"""The server and lane API share one validated content-addressed blob namespace."""

import hashlib
from collections.abc import AsyncIterator

from forge.api.lane_v1_blobs import BlobError, LaneBlobStore


class Blobs:
    def __init__(self, lane: LaneBlobStore) -> None:
        self.lane = lane

    async def put(self, content: bytes) -> str:
        digest = hashlib.sha256(content).hexdigest()

        async def chunks() -> AsyncIterator[bytes]:
            yield content

        await self.lane.put(digest, chunks())
        return digest

    async def get(self, digest: str) -> bytes | None:
        try:
            return await self.lane.read(digest)
        except BlobError as error:
            if str(error) == "blob is not present":
                return None
            raise
