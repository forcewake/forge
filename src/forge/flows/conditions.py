"""Safe expression evaluator for flow step conditions.

Supports a restricted subset of Python expressions:
    "review.severity != 'critical'"
    "security.confirmed_count == 0"
    "pipeline.status == 'success'"
    "always"  / "never"
    Boolean operators: and, or, not

Uses AST parsing with an allowlisted node visitor — no arbitrary code execution.
"""

from __future__ import annotations

import ast
import logging
import operator
from typing import Any

logger = logging.getLogger(__name__)

_COMPARE_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.Gt: operator.gt,
    ast.LtE: operator.le,
    ast.GtE: operator.ge,
}


def evaluate_condition(expression: str, state: dict) -> bool:
    """Evaluate a condition expression against flow state.

    Returns False on any error (safe default).
    """
    expression = expression.strip()

    if not expression or expression == "always":
        return True
    if expression == "never":
        return False

    try:
        tree = ast.parse(expression, mode="eval")
        return bool(_eval_node(tree.body, state))
    except Exception:
        logger.warning("Condition evaluation failed for: %s", expression)
        return False


def _resolve_name(node: ast.expr, state: dict) -> Any:
    """Resolve a dotted name (e.g. review.severity) against state dict."""
    if isinstance(node, ast.Name):
        return state.get(node.id)
    if isinstance(node, ast.Attribute):
        parent = _resolve_name(node.value, state)
        if isinstance(parent, dict):
            return parent.get(node.attr)
        return None
    raise ValueError(f"Unsupported name node: {type(node).__name__}")


def _eval_node(node: ast.expr, state: dict) -> Any:
    """Recursively evaluate an AST node."""
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, (ast.Name, ast.Attribute)):
        return _resolve_name(node, state)

    if isinstance(node, ast.Compare):
        left = _eval_node(node.left, state)
        for op, comparator in zip(node.ops, node.comparators):
            right = _eval_node(comparator, state)
            op_type = type(op)
            if op_type in _COMPARE_OPS:
                if not _COMPARE_OPS[op_type](left, right):
                    return False
            elif isinstance(op, ast.In):
                if left not in right:
                    return False
            elif isinstance(op, ast.NotIn):
                if left in right:
                    return False
            else:
                raise ValueError(f"Unsupported comparison: {type(op).__name__}")
            left = right
        return True

    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(_eval_node(v, state) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(_eval_node(v, state) for v in node.values)
        raise ValueError(f"Unsupported bool op: {type(node.op).__name__}")

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _eval_node(node.operand, state)

    raise ValueError(f"Unsupported AST node: {type(node).__name__}")
