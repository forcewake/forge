"""Canonicalize PostgreSQL's two equivalent literal VARCHAR-to-TEXT array forms."""

import re

_LITERAL = r"'(?:[^']|'')*'"
_VARCHAR = _LITERAL + r"::character varying"
_DISTRIBUTED = r"(?:\(" + _VARCHAR + r"\)|" + _VARCHAR + r")::text"
_VALUES = _VARCHAR + r"(?:,\s*" + _VARCHAR + r")*"
_TEXT_VALUES = _DISTRIBUTED + r"(?:,\s*" + _DISTRIBUTED + r")*"
_ARRAY = re.compile(
    r"\(ARRAY\["
    + _VALUES
    + r"\]\)::text\[\]|ARRAY\["
    + _VALUES
    + r"\]::text\[\]|ARRAY\["
    + _TEXT_VALUES
    + r"\]"
)
_QUOTED = re.compile(r"[Ee]'(?:\\.|[^'\\]|'')*'|'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"")
_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")


def canonical_sql(expression: str) -> str:
    output, position = [], 0
    while position < len(expression):
        dollar = _DOLLAR.match(expression, position)
        if dollar is not None:
            end = expression.find(dollar[0], dollar.end())
            if end < 0:
                return expression
            output.append(expression[position : end + len(dollar[0])])
            position = end + len(dollar[0])
            continue
        quoted = _QUOTED.match(expression, position)
        if quoted is not None:
            output.append(quoted[0])
            position = quoted.end()
            continue
        match = _ARRAY.match(expression, position)
        if match is not None and (
            position == 0
            or not (expression[position - 1].isalnum() or expression[position - 1] == "_")
        ):
            literals = re.findall(_LITERAL, match[0])
            output.append("ARRAY[" + ", ".join(value + "::text" for value in literals) + "]")
            position = match.end()
            continue
        output.append(expression[position])
        position += 1
    return "".join(output)
