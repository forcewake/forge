from forge.flows.templates import render_template


class TestRenderTemplate:
    """Tests for the flow template engine."""

    def test_simple_substitution(self):
        state = {"review": {"summary": "Looks good"}}
        result = render_template("Result: {review.summary}", state)
        assert result == "Result: Looks good"

    def test_multiple_placeholders(self):
        state = {
            "review": {"severity": "warning", "summary": "Minor issues"},
            "security": {"risk_level": "low"},
        }
        template = "Code: {review.severity} | Security: {security.risk_level}"
        result = render_template(template, state)
        assert result == "Code: warning | Security: low"

    def test_missing_key_left_intact(self):
        result = render_template("Value: {missing.key}", {})
        assert result == "Value: {missing.key}"

    def test_partial_path_missing(self):
        state = {"review": {"summary": "ok"}}
        result = render_template("{review.nonexistent}", state)
        assert result == "{review.nonexistent}"

    def test_top_level_key(self):
        state = {"name": "test-flow"}
        result = render_template("Flow: {name}", state)
        assert result == "Flow: test-flow"

    def test_deeply_nested(self):
        state = {"a": {"b": {"c": {"d": "deep"}}}}
        result = render_template("{a.b.c.d}", state)
        assert result == "deep"

    def test_numeric_value(self):
        state = {"count": 42}
        result = render_template("Count: {count}", state)
        assert result == "Count: 42"

    def test_no_placeholders(self):
        result = render_template("No placeholders here", {"key": "val"})
        assert result == "No placeholders here"

    def test_empty_template(self):
        result = render_template("", {"key": "val"})
        assert result == ""

    def test_mixed_found_and_missing(self):
        state = {"found": "yes"}
        result = render_template("{found} and {missing}", state)
        assert result == "yes and {missing}"

    def test_multiline_template(self):
        state = {"review": {"summary": "All good"}, "security": {"summary": "No issues"}}
        template = "Review: {review.summary}\nSecurity: {security.summary}"
        result = render_template(template, state)
        assert result == "Review: All good\nSecurity: No issues"
