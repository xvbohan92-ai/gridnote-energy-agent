import pytest

from energy_agent.evaluation import compare_rows, score_case


@pytest.mark.parametrize("code", ["provider_error", "query_error", "configuration_error", "semantic_error"])
def test_system_error_never_counts_as_correct_refusal(code):
    case = {"kind": "reject", "expected_outcome": "refused", "expected_error": "data_unavailable"}
    response = {"status": "error", "outcome": "failed", "error_code": code}
    assert score_case(case, response, []) is False


def test_refusal_reason_must_match():
    case = {"kind": "reject", "expected_outcome": "refused", "expected_error": "policy_refusal"}
    assert not score_case(case, {"outcome": "refused", "error_code": "data_unavailable"}, [])
    assert score_case(case, {"outcome": "refused", "error_code": "policy_refusal"}, [])


def test_nan_infinity_missing_null_columns_are_not_correct():
    assert not compare_rows([{"value": 1.0}], [{"value": float("nan")}])
    assert not compare_rows([{"value": 1.0}], [{"value": float("inf")}])
    assert not compare_rows([{"value": None}], [{}])
