"""Bounded pure Git binary-patch decoding; the native blob OID verifies its result."""

import base64
from dataclasses import dataclass
from typing import Literal
import zlib

from .blob import BlobContent
from .patch import CandidateError

MAX_BINARY_BYTES = 256 * 1024
MAX_ENCODING_BYTES = 2 * MAX_BINARY_BYTES + 64


@dataclass(frozen=True)
class BinaryBlock:
    kind: Literal["literal", "delta"]
    size: int
    compressed: BlobContent

    def decoded(self) -> bytes:
        if not 0 <= self.size <= MAX_ENCODING_BYTES:
            raise CandidateError("binary_too_large", "binary instruction size exceeds bound")
        try:
            decoder = zlib.decompressobj()
            raw = decoder.decompress(self.compressed.raw, self.size + 1)
            if (
                len(raw) != self.size
                or not decoder.eof
                or decoder.unused_data
                or decoder.unconsumed_tail
            ):
                raise ValueError("binary compressed stream differs from its bound")
            return raw
        except (zlib.error, ValueError):
            raise CandidateError(
                "corrupt_binary_patch", "invalid bounded binary instruction stream"
            ) from None

    def apply(self, base: bytes) -> bytes:
        raw = self.decoded()
        output = raw if self.kind == "literal" else apply_delta(base, raw)
        if len(output) > MAX_BINARY_BYTES:
            raise CandidateError("binary_too_large", "binary post-image exceeds write bound")
        return output


def _length(char: str) -> int:
    if "A" <= char <= "Z":
        return ord(char) - ord("A") + 1
    if "a" <= char <= "z":
        return ord(char) - ord("a") + 27
    raise ValueError("invalid binary line length")


def parse_binary_blocks(lines: tuple[str, ...]) -> tuple[BinaryBlock, BinaryBlock]:
    blocks: list[BinaryBlock] = []
    offset = 0
    try:
        while offset < len(lines):
            if not lines[offset]:
                offset += 1
                continue
            kind, size = lines[offset].split(" ")
            if kind not in ("literal", "delta") or not size.isascii() or not size.isdecimal():
                raise ValueError("invalid binary block header")
            offset += 1
            compressed = bytearray()
            while offset < len(lines) and lines[offset]:
                line = lines[offset]
                count = _length(line[0])
                if len(line[1:]) != 5 * ((count + 3) // 4):
                    raise ValueError("invalid binary line width")
                data = base64.b85decode(line[1:].encode("ascii"))
                if any(data[count:]):
                    raise ValueError("nonzero binary line padding")
                compressed.extend(data[:count])
                if len(compressed) > MAX_ENCODING_BYTES:
                    raise ValueError("binary encoded stream exceeds bound")
                offset += 1
            block = BinaryBlock(
                "literal" if kind == "literal" else "delta",
                int(size),
                BlobContent(raw=bytes(compressed)),
            )
            block.decoded()
            blocks.append(block)
            if len(blocks) > 2:
                raise ValueError("extra binary block")
        if len(blocks) != 2:
            raise ValueError("forward and reverse binary blocks required")
        return blocks[0], blocks[1]
    except (ValueError, UnicodeError, IndexError):
        raise CandidateError("corrupt_binary_patch", "invalid Git binary patch encoding") from None


def _varint(data: bytes, offset: int) -> tuple[int, int]:
    value, shift = 0, 0
    while offset < len(data) and shift <= 63:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise CandidateError("corrupt_binary_patch", "invalid binary delta size")


def apply_delta(base: bytes, instructions: bytes) -> bytes:
    source_size, offset = _varint(instructions, 0)
    result_size, offset = _varint(instructions, offset)
    if source_size != len(base):
        raise CandidateError("stale_base", "binary delta source size differs from exact base")
    if result_size > MAX_BINARY_BYTES:
        raise CandidateError("binary_too_large", "binary delta post-image exceeds bound")
    output = bytearray()
    while offset < len(instructions):
        command = instructions[offset]
        offset += 1
        if command & 0x80:
            start, size = 0, 0
            for bit, shift in ((1, 0), (2, 8), (4, 16), (8, 24), (16, 0), (32, 8), (64, 16)):
                if command & bit:
                    if offset >= len(instructions):
                        raise CandidateError(
                            "corrupt_binary_patch", "truncated binary copy instruction"
                        )
                    byte = instructions[offset] << shift
                    offset += 1
                    if bit < 16:
                        start |= byte
                    else:
                        size |= byte
            size = size or 0x10000
            if start + size > len(base):
                raise CandidateError("corrupt_binary_patch", "binary copy exceeds exact base")
            output.extend(base[start : start + size])
        elif command:
            end = offset + command
            if end > len(instructions):
                raise CandidateError("corrupt_binary_patch", "truncated binary insert")
            output.extend(instructions[offset:end])
            offset = end
        else:
            raise CandidateError("corrupt_binary_patch", "binary delta contains zero opcode")
        if len(output) > result_size:
            raise CandidateError("corrupt_binary_patch", "binary delta exceeds claimed result")
    if len(output) != result_size:
        raise CandidateError("corrupt_binary_patch", "binary delta result size disagrees")
    return bytes(output)
