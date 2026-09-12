from forge.flows.conditions import evaluate_condition


class TestEvaluateCondition:
    """Tests for the safe condition evaluator."""

    def test_always(self):
        assert evaluate_condition("always", {}) is True

    def test_never(self):
        assert evaluate_condition("never", {}) is False

    def test_empty_string(self):
        assert evaluate_condition("", {}) is True

    def test_equality_true(self):
        state = {"review": {"severity": "warning"}}
        assert evaluate_condition("review.severity == 'warning'", state) is True

    def test_equality_false(self):
        state = {"review": {"severity": "critical"}}
        assert evaluate_condition("review.severity == 'warning'", state) is False

    def test_not_equal_true(self):
        state = {"review": {"severity": "warning"}}
        assert evaluate_condition("review.severity != 'critical'", state) is True

    def test_not_equal_false(self):
        state = {"review": {"severity": "critical"}}
        assert evaluate_condition("review.severity != 'critical'", state) is False

    def test_numeric_equality(self):
        state = {"security": {"confirmed_count": 0}}
        assert evaluate_condition("security.confirmed_count == 0", state) is True

    def test_numeric_greater_than(self):
        state = {"security": {"confirmed_count": 3}}
        assert evaluate_condition("security.confirmed_count > 0", state) is True

    def test_numeric_less_than(self):
        state = {"security": {"confirmed_count": 0}}
        assert evaluate_condition("security.confirmed_count < 5", state) is True

    def test_boolean_and_true(self):
        state = {"a": {"x": 1}, "b": {"y": 2}}
        assert evaluate_condition("a.x == 1 and b.y == 2", state) is True

    def test_boolean_and_false(self):
        state = {"a": {"x": 1}, "b": {"y": 3}}
        assert evaluate_condition("a.x == 1 and b.y == 2", state) is False

    def test_boolean_or(self):
        state = {"a": {"x": 1}, "b": {"y": 3}}
        assert evaluate_condition("a.x == 99 or b.y == 3", state) is True

    def test_not_operator(self):
        state = {"done": True}
        assert evaluate_condition("not done", state) is False

    def test_in_operator(self):
        state = {"items": ["a", "b", "c"]}
        assert evaluate_condition("'a' in items", state) is True

    def test_not_in_operator(self):
        state = {"items": ["a", "b", "c"]}
        assert evaluate_condition("'z' not in items", state) is True

    def test_dotted_path_nested(self):
        state = {"pipeline": {"status": "success"}}
        assert evaluate_condition("pipeline.status == 'success'", state) is True

    def test_missing_key_returns_false(self):
        assert evaluate_condition("missing.key == 'value'", {}) is False

    def test_invalid_expression_returns_false(self):
        assert evaluate_condition("not a valid <<<", {}) is False

    def test_function_call_rejected(self):
        assert evaluate_condition("print('hello')", {}) is False

    def test_import_rejected(self):
        assert evaluate_condition("__import__('os')", {}) is False

    def test_lambda_rejected(self):
        assert evaluate_condition("lambda: True", {}) is False

    def test_top_level_name(self):
        state = {"ready": True}
        assert evaluate_condition("ready", state) is True

    def test_top_level_name_false(self):
        state = {"ready": False}
        assert evaluate_condition("ready", state) is False
