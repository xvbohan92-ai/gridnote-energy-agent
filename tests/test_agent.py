import sqlite3

from energy_agent.agent import ask
from energy_agent.evaluation import CASES, compare_rows, evaluate
from energy_agent.models import AskRequest, QueryPlan
from energy_agent.planner import DemoPlanner


class CorrectingPlanner(DemoPlanner):
    def __init__(self, *, always_wrong=False):
        super().__init__()
        self.attempts = 0
        self.always_wrong = always_wrong

    def plan(self, question, context, scope, previous=None):
        self.attempts += 1
        if self.attempts == 1 or self.always_wrong:
            return QueryPlan(
                title="test",
                sql="SELECT missing_column FROM load_hourly",
                chart="none",
                x="",
                y=[],
                note="injected schema failure",
            )
        assert "missing_column" in previous["error"]
        return QueryPlan(
            title="test",
            sql="SELECT COUNT(*) AS n FROM load_hourly",
            chart="none",
            x="",
            y=[],
            note="fixed schema",
        )


def test_retry_recovers_from_a_real_schema_error(settings):
    result = ask(settings, AskRequest(question="负荷记录数"), planner=CorrectingPlanner())
    assert result["status"] == "ok"
    assert result["result"]["rows"] == [{"n": 168}]
    assert result["retries"] == 1
    assert [s["node"] for s in result["trace"]].count("repair") == 1


def test_baseline_fails_without_repair(settings):
    result = ask(settings, AskRequest(question="负荷记录数"), planner=CorrectingPlanner(), baseline=True)
    assert result["status"] == "error"
    assert result["retries"] == 0


def test_retry_is_bounded(settings):
    planner = CorrectingPlanner(always_wrong=True)
    result = ask(settings, AskRequest(question="负荷记录数"), planner=planner)
    assert result["status"] == "error"
    assert result["retries"] == 2
    assert planner.attempts == 3


def test_missing_predictions_are_not_zero_error(settings):
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE load_hourly SET forecast_mw=NULL")
    result = ask(settings, AskRequest(question="最近7天预测误差"))
    assert result["result"]["rows"][0]["mae_mw"] is None
    assert result["result"]["rows"][0]["rmse_mw"] is None
    assert result["result"]["rows"][0]["forecast_hours"] == 0
    assert "无法计算" in result["answer"]
    assert result["chart"] is None


def test_no_results_never_widens_scope_or_retries(settings):
    with sqlite3.connect(settings.db_path) as conn:
        conn.execute("UPDATE load_hourly SET load_mw=100, forecast_mw=100")
    result = ask(settings, AskRequest(question="最近7天负荷异常"))
    assert result["status"] == "empty"
    assert result["retries"] == 0
    assert not result["result"]["rows"]


def test_numeric_comparator_rejects_wrong_results():
    assert not compare_rows([{"mae": 5.0}], [{"mae": 7.0}])
    assert not compare_rows([{"n": 1}], [])


def test_benchmark_checks_numeric_gold_and_rejections(tmp_path):
    report = evaluate(output=tmp_path / "report.json")
    assert report["summary"]["agent"]["correct"] == len(CASES)
    assert report["summary"]["baseline"]["correct"] == len(CASES)
    assert report["summary"]["domain"]["correct"] == len(CASES)
    assert report["summary"]["agent"]["model_calls"] == 0
    assert report["semantic_diagnostics"]["summary"]["domain"]["correct"] == 4
    assert report["semantic_diagnostics"]["summary"]["agent"]["correct"] == 0
    assert (tmp_path / "report.json").exists()
