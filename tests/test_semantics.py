import sqlite3
from contextlib import closing

import pytest

from energy_agent.agent import ask
from energy_agent.errors import SemanticError
from energy_agent.evaluation import InjectedSemanticPlanner
from energy_agent.metrics import verify_result
from energy_agent.models import AnalysisTask, AskRequest
from energy_agent.planner import DemoPlanner, PlanningError
from energy_agent.tasks import parse_task


@pytest.mark.parametrize(
    "question,field,value",
    [
        ("最近7天负荷最高的10个小时", "top_k", 10),
        ("top 12 peak load hours", "top_k", 12),
        ("按周的负荷趋势", "granularity", "week"),
        ("工作日与周末负荷比较", "granularity", "day_type"),
        ("周末预测误差", "day_filter", "weekend"),
        ("整体预测误差", "granularity", "total"),
        ("总用电量 MWh", "unit", "MWh"),
    ],
)
def test_task_parameters_are_explicit(question, field, value):
    assert getattr(parse_task(question), field) == value


@pytest.mark.parametrize("question", ["负荷 top 0", "负荷 top 101", "负荷 top -1", "负荷 top 3.5"])
def test_invalid_topk_is_not_silently_replaced(question):
    with pytest.raises(PlanningError) as error:
        parse_task(question)
    assert error.value.code == "unsupported_question"


@pytest.mark.parametrize("question", ["负荷增长率", "负荷异常原因", "每日最高负荷", "预测误差最大的小时"])
def test_unsupported_metrics_do_not_inherit_a_false_contract(question):
    assert parse_task(question).kind == "custom"


@pytest.mark.parametrize(
    "fault,question",
    [
        ("power_sum", "最近7天负荷趋势"),
        ("energy_average", "最近7天每日电量"),
        ("wrong_topk", "最近7天负荷 top 10"),
    ],
)
def test_domain_repairs_executable_but_wrong_sql(settings, fault, question):
    result = ask(settings, AskRequest(question=question), planner=InjectedSemanticPlanner(fault))
    assert result["outcome"] == "success"
    assert result["semantic_check"]["status"] == "verified"
    assert result["retries"] == 1
    errors = [step for step in result["trace"] if step["node"] == "semantics" and step["status"] == "error"]
    assert len(errors) == 1
    assert result["error_code"] is None
    if fault == "wrong_topk":
        assert len(result["result"]["rows"]) == 10


def test_basic_agent_cannot_detect_a_wrong_but_executable_aggregate(settings):
    result = ask(
        settings,
        AskRequest(question="最近7天负荷趋势"),
        planner=InjectedSemanticPlanner("power_sum"),
        mode="agent",
    )
    assert result["outcome"] == "success"
    assert result["retries"] == 0
    assert result["semantic_check"]["status"] == "not_run"
    assert result["result"]["rows"][0]["load_mw"] > 1_000_000


def test_null_denominator_repaired_and_coverage_is_explicit(settings):
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE load_hourly SET forecast_mw=NULL WHERE timestamp_utc LIKE '2025-01-22%'")
        conn.execute("UPDATE load_hourly SET load_mw=0 WHERE timestamp_utc='2025-01-23T00:00:00Z'")
    result = ask(
        settings, AskRequest(question="最近7天预测误差"), planner=InjectedSemanticPlanner("null_as_zero")
    )
    assert result["semantic_check"]["status"] == "verified"
    assert result["retries"] == 1
    assert result["quality"]["forecast_hours"] == 144
    assert result["quality"]["mape_hours"] == 143
    assert result["result"]["rows"][0]["mae_mw"] is None
    assert result["result"]["rows"][0]["mape_hours"] == 0


def test_forecast_with_no_observations_is_insufficient_not_success(settings):
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("UPDATE load_hourly SET forecast_mw=NULL")
    result = ask(settings, AskRequest(question="预测误差"))
    assert result["outcome"] == "insufficient_data"
    assert result["semantic_check"]["status"] == "verified"


def test_partial_day_reports_missing_hours_in_requested_window(settings):
    with closing(sqlite3.connect(settings.db_path)) as conn, conn:
        conn.execute("DELETE FROM load_hourly WHERE timestamp_utc='2025-01-23T01:00:00Z'")
    result = ask(settings, AskRequest(question="最近7天总电量"))
    assert result["quality"]["missing_hours"] == 1
    assert result["quality"]["expected_hours"] == 168
    assert result["quality"]["observed_hours"] == 167
    assert result["semantic_check"]["status"] == "verified"


class AlwaysWrongPlanner(InjectedSemanticPlanner):
    def plan(self, question, context, scope, previous=None):
        return super().plan(question, context, scope, previous=None)


def test_failed_semantics_never_publishes_wrong_data_or_chart(settings):
    result = ask(settings, AskRequest(question="负荷趋势"), planner=AlwaysWrongPlanner("power_sum"))
    assert result["outcome"] == "failed"
    assert result["error_code"] == "semantic_error"
    assert result["retries"] == 2
    assert result["result"]["rows"] == []
    assert result["chart"] is None


def test_uncovered_custom_query_is_labeled(settings):
    class CustomPlanner(DemoPlanner):
        def plan(self, *args, **kwargs):
            return super().plan("负荷记录数", *args[1:], **kwargs)

    result = ask(settings, AskRequest(question="负荷增长率"), planner=CustomPlanner())
    assert result["task"]["kind"] == "custom"
    assert result["semantic_check"]["status"] == "not_covered"


def test_explicit_model_limit_keeps_truthful_truncation(settings):
    class LimitedPlanner(DemoPlanner):
        def plan(self, *args, **kwargs):
            plan = super().plan(*args, **kwargs)
            return plan.model_copy(update={"sql": plan.sql + " LIMIT 500"})

    result = ask(
        settings, AskRequest(question="2025-01-01 到 2025-01-28 逐小时负荷"), planner=LimitedPlanner()
    )
    assert result["semantic_check"]["status"] == "verified"
    assert result["result"]["truncated"] is True
    assert len(result["result"]["rows"]) == 500
    assert result["retries"] == 0


def test_empty_results_still_require_the_contract_columns():
    with pytest.raises(SemanticError, match="缺少"):
        verify_result([], AnalysisTask(kind="trend"), {"columns": ["fake"], "rows": [], "truncated": False})


def test_verified_presentation_cannot_use_unverified_extra_values(settings):
    class MisleadingPlanner(DemoPlanner):
        def plan(self, *args, **kwargs):
            plan = super().plan(*args, **kwargs)
            return plan.model_copy(
                update={
                    "sql": plan.sql.replace("SELECT ", "SELECT 42 AS fake, ", 1),
                    "title": "错误的业务标题",
                    "y": ["fake"],
                }
            )

    result = ask(settings, AskRequest(question="负荷趋势"), planner=MisleadingPlanner())
    assert result["semantic_check"]["status"] == "verified"
    assert result["chart"]["y"] == ["load_mw", "forecast_mw"]
    assert result["title"] == "每日平均负荷趋势"
