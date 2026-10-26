import pytest

from energy_agent.models import AskRequest
from energy_agent.scope import ScopeError, resolve_scope


def test_relative_dates_use_latest_dataset_day(context):
    scope = resolve_scope(AskRequest(question="最近7天负荷趋势"), context)
    assert scope.start == "2025-01-22T00:00:00Z"
    assert scope.end == "2025-01-29T00:00:00Z"
    assert "最新数据日" in scope.note


def test_chinese_adjacent_iso_dates(context):
    scope = resolve_scope(AskRequest(question="请查2025-01-01到2025-01-02的负荷趋势"), context)
    assert scope.start.startswith("2025-01-01")
    assert scope.end.startswith("2025-01-03")


def test_previous_week_is_a_calendar_week(context):
    scope = resolve_scope(AskRequest(question="上周负荷趋势"), context)
    assert scope.start == "2025-01-20T00:00:00Z"
    assert scope.end == "2025-01-27T00:00:00Z"


@pytest.mark.parametrize(
    "query_request",
    [
        AskRequest(question="2030-01-01的负荷"),
        AskRequest(question="2025-02-30 的负荷"),
        AskRequest(question="负荷趋势", start_date="2025-01-10", end_date="2025-01-01"),
        AskRequest(question="最近0天的负荷"),
        AskRequest(question="最近999天的负荷"),
        AskRequest(question="最近7天负荷", start_date="2025-01-01", end_date="2025-01-02"),
        AskRequest(question="2025-01-01 的负荷", start_date="2025-01-03", end_date="2025-01-04"),
        AskRequest(question="DOM 和 AECO 的负荷"),
        AskRequest(question="DOM 的负荷", region="AECO"),
        AskRequest(question="负荷趋势", region="not-a-real-region"),
        AskRequest(question="今天的负荷趋势"),
        AskRequest(question="2025年1月的负荷"),
        AskRequest(question="2025-1-1的负荷"),
        AskRequest(question="最近三天的负荷"),
        AskRequest(question="所有区域的负荷"),
        AskRequest(question="负荷趋势", start_date="2025-01-01"),
    ],
)
def test_ambiguous_conflicting_or_invalid_scopes_rejected(context, query_request):
    with pytest.raises(ScopeError):
        resolve_scope(query_request, context)
