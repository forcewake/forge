"""Decode native Git C-quoted paths without shell parsing or a lossy codec."""

from .patch import CandidateError

_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}


def _quoted_end(value: str) -> int:
    escaped = False
    for index, char in enumerate(value[1:], 1):
        if char == '"' and not escaped:
            return index + 1
        escaped = char == "\\" and not escaped
    raise CandidateError("malformed_diff", "unterminated Git path quote")


def decode_path(value: str, *, wire_bytes: bool = False) -> str:
    raw = bytearray()
    if value.startswith('"'):
        if _quoted_end(value) != len(value):
            raise CandidateError("malformed_diff", "Git path quote has trailing data")
        text, offset = value[1:-1], 0
        while offset < len(text):
            char = text[offset]
            offset += 1
            if char == "\\":
                if offset >= len(text):
                    raise CandidateError("malformed_diff", "truncated Git path escape")
                char = text[offset]
                offset += 1
                if char in "01234567":
                    digits = char + text[offset : offset + 2]
                    if (
                        len(digits) != 3
                        or any(c not in "01234567" for c in digits)
                        or int(digits, 8) > 255
                    ):
                        raise CandidateError("malformed_diff", "invalid Git octal path escape")
                    offset += 2
                    raw.append(int(digits, 8))
                elif char in _ESCAPES:
                    raw.append(_ESCAPES[char])
                else:
                    raise CandidateError("malformed_diff", "invalid Git path escape")
            else:
                raw.extend(char.encode("latin1" if wire_bytes else "utf-8"))
    else:
        raw.extend(value.encode("latin1" if wire_bytes else "utf-8"))
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise CandidateError(
            "unsupported_path_encoding", "repository path must be valid UTF-8"
        ) from None
    return path[2:] if path.startswith(("a/", "b/")) else path


def header_path(line: str, *, wire_bytes: bool = False) -> str:
    rest = line.removeprefix("diff --git ")
    if rest.startswith('"'):
        end = _quoted_end(rest)
        left, right = rest[:end], rest[end:].lstrip(" ")
        if not right:
            raise CandidateError("malformed_diff", "Git diff header requires two paths")
        decode_path(left, wire_bytes=wire_bytes)
        return decode_path(right, wire_bytes=wire_bytes)
    if ' "b/' in rest:
        left, right = rest.split(' "b/', 1)
        decode_path(left, wire_bytes=wire_bytes)
        return decode_path('"b/' + right, wire_bytes=wire_bytes)
    choices = [
        (rest[:index], rest[index + 1 :])
        for index in range(len(rest))
        if rest.startswith(" b/", index)
    ]
    exact = [right for left, right in choices if left.startswith("a/") and left[2:] == right[2:]]
    if len(exact) == 1:
        return decode_path(exact[0], wire_bytes=wire_bytes)
    if len(choices) == 1:
        return decode_path(choices[0][1], wire_bytes=wire_bytes)
    raise CandidateError("malformed_diff", "Git diff header path is ambiguous")
