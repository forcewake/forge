"""Atomic, bounded content-addressed storage; authorization belongs to the run row."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
import tempfile
from collections.abc import AsyncIterable
from pathlib import Path


class BlobError(ValueError):
    pass


class BlobTooLarge(BlobError):
    pass


class LaneBlobStore:
    def __init__(self, root: Path, *, max_bytes: int = 16 * 1024 * 1024):
        if not 1 <= max_bytes <= 64 * 1024 * 1024:
            raise ValueError("invalid lane blob size limit")
        self.root = root.resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        if re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise BlobError("invalid blob digest")
        return self.root / digest

    async def put(self, digest: str, chunks: AsyncIterable[bytes]) -> int:
        destination = self._path(digest)
        fd, temporary = tempfile.mkstemp(prefix=".upload-", dir=self.root)
        count, hasher = 0, hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as stream:
                async for chunk in chunks:
                    if count + len(chunk) > self.max_bytes:
                        raise BlobTooLarge("lane blob exceeds the upload limit")
                    count += len(chunk)
                    hasher.update(chunk)
                    await asyncio.to_thread(stream.write, chunk)
                await asyncio.to_thread(stream.flush)
                await asyncio.to_thread(os.fsync, stream.fileno())
            if hasher.hexdigest() != digest:
                raise BlobError("blob content does not match its digest")
            try:
                os.link(temporary, destination, follow_symlinks=False)
                directory = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError:
                if len(await self.read(digest)) != count:
                    raise BlobError("stored blob has a different size") from None
            return count
        finally:
            Path(temporary).unlink(missing_ok=True)

    async def discard(self, digest: str) -> None:
        await asyncio.to_thread(self._path(digest).unlink, missing_ok=True)

    async def read(self, digest: str) -> bytes:
        return await asyncio.to_thread(self._read, self._path(digest), digest)

    def _read(self, path: Path, digest: str) -> bytes:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_bytes:
                    raise BlobError("stored blob is not a bounded regular file")
                body = stream.read(self.max_bytes + 1)
        except FileNotFoundError:
            raise BlobError("blob is not present") from None
        except OSError:
            raise BlobError("blob cannot be read safely") from None
        if len(body) > self.max_bytes or hashlib.sha256(body).hexdigest() != digest:
            raise BlobError("stored blob digest is invalid")
        return body
