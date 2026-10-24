"""Native dump/restore cast spelling cannot erase meaningful SQL differences."""

import pytest

from forge.store.schema.expressions import canonical_sql


@pytest.mark.parametrize(
    "expression",
    (
        "ARRAY['pending'::character varying, 'done'::character varying]::text[]",
        "(ARRAY['pending'::character varying, 'done'::character varying])::text[]",
        "ARRAY['pending'::character varying::text, 'done'::character varying::text]",
        "ARRAY[('pending'::character varying)::text, ('done'::character varying)::text]",
    ),
)
def test_native_equivalent_cast_spellings_have_one_exact_value_sequence(expression):
    assert canonical_sql(expression) == "ARRAY['pending'::text, 'done'::text]"


@pytest.mark.parametrize(
    "expression",
    (
        "ARRAY['pending'::character varying(3)]::text[]",
        "ARRAY[state::character varying]::text[]",
        "ARRAY['pending'::character varying, state::character varying]::text[]",
        "ARRAY['pending'::varchar]::text[]",
        "\"ARRAY['pending'::character varying]::text[]\"",
        "'ARRAY[''pending''::character varying]::text[]'",
        r"E'escaped\'ARRAY[''pending''::character varying]::text[]'",
        "$$ARRAY['pending'::character varying]::text[]$$",
        "$body$ARRAY['pending'::character varying]::text[]$body$",
        "$unterminated$ARRAY['pending'::character varying]::text[]",
        "MY_ARRAY['pending'::character varying]::text[]",
    ),
)
def test_cast_canonicalization_preserves_other_grammar_and_all_quoted_contents(expression):
    assert canonical_sql(expression) == expression


def test_changed_literals_remain_different_after_native_cast_normalization():
    first = "ARRAY['pending'::character varying, 'done'::character varying]::text[]"
    second = "ARRAY['pending'::character varying::text, 'failed'::character varying::text]"
    assert canonical_sql(first) != canonical_sql(second)
