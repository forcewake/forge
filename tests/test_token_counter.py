from forge.context.token_counter import DEFAULT_BUDGETS, TokenBudget, TokenCounter


class TestTokenCounter:
    def test_count_basic(self):
        counter = TokenCounter()
        count = counter.count("hello world")
        assert count == 2  # cl100k_base encodes "hello world" as 2 tokens

    def test_count_empty(self):
        counter = TokenCounter()
        assert counter.count("") == 0

    def test_truncate_no_op_when_within_budget(self):
        counter = TokenCounter()
        text = "hello world"
        result = counter.truncate(text, max_tokens=100)
        assert result == text  # no truncation needed

    def test_truncate_tail_strategy(self):
        counter = TokenCounter()
        # Generate text that's definitely more than 5 tokens
        text = " ".join(f"word{i}" for i in range(100))
        result = counter.truncate(text, max_tokens=5, strategy="tail")
        assert result.endswith("... [truncated]")
        assert counter.count(result) < counter.count(text)

    def test_truncate_head_strategy(self):
        counter = TokenCounter()
        text = " ".join(f"word{i}" for i in range(100))
        result = counter.truncate(text, max_tokens=5, strategy="head")
        assert result.startswith("[truncated] ...")
        assert counter.count(result) < counter.count(text)

    def test_truncate_middle_strategy(self):
        counter = TokenCounter()
        text = " ".join(f"word{i}" for i in range(100))
        result = counter.truncate(text, max_tokens=10, strategy="middle")
        assert "[truncated middle]" in result
        assert counter.count(result) < counter.count(text)

    def test_truncate_zero_budget(self):
        counter = TokenCounter()
        assert counter.truncate("hello", max_tokens=0) == ""


class TestTokenBudget:
    def test_default_budgets(self):
        budget = TokenBudget()
        assert budget.total_budget == DEFAULT_BUDGETS["total"]

    def test_custom_budgets_override(self):
        budget = TokenBudget({"total": 10_000, "diff": 5_000})
        assert budget.total_budget == 10_000
        assert budget.remaining("diff") == 5_000

    def test_remaining_decreases(self):
        budget = TokenBudget({"total": 100, "diff": 50})
        budget.consume("diff", 30)
        assert budget.remaining("diff") == 20
        assert budget.total_used == 30

    def test_consume_clamps_to_remaining(self):
        budget = TokenBudget({"total": 100, "diff": 50})
        actual = budget.consume("diff", 999)
        assert actual == 50
        assert budget.remaining("diff") == 0

    def test_total_budget_caps_category(self):
        budget = TokenBudget({"total": 30, "diff": 50})
        # Category has 50 but total only has 30
        assert budget.remaining("diff") == 30

    def test_fit_truncates_and_consumes(self):
        budget = TokenBudget({"total": 1000, "diff": 10})
        text = " ".join(f"word{i}" for i in range(200))
        result = budget.fit(text, "diff", strategy="tail")
        assert budget.remaining("diff") >= 0
        assert budget.total_used > 0
        assert len(result) < len(text)

    def test_fit_returns_empty_when_budget_exhausted(self):
        budget = TokenBudget({"total": 100, "diff": 50})
        budget.consume("diff", 50)
        result = budget.fit("some text here", "diff")
        assert result == ""

    def test_cumulative_usage_across_categories(self):
        budget = TokenBudget({"total": 100, "diff": 60, "description": 60})
        budget.consume("diff", 50)
        assert budget.remaining("description") == 50  # 100 - 50 = 50 total remaining
