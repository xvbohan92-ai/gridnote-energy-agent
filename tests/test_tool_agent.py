import json
from dataclasses import replace

import httpx
import pytest
from openai import OpenAI

from energy_agent.models import AskRequest
from energy_agent.tasks import compile_task, parse_task
from energy_agent.tool_agent import ResponsesToolModel, ToolRuntime, ask_tool_agent


class ScriptedModel:
    def __init__(self, turns):
        self.turns, self.inputs = turns, []
        self.usage = {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": None}

    def respond(self, transcript, remaining):
        self.inputs.append(list(transcript))
        index = self.usage["calls"]
        self.usage["calls"] += 1
        name, args = self.turns[index]
        return [
            {"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": f"call_{index}"}
        ]


def test_model_selects_tools_and_receives_real_observations(settings):
    model = ScriptedModel(
        [("inspect_dataset", {}), ("calculate_metric", {}), ("finish_analysis", {"evidence_id": "e1"})]
    )
    response = ask_tool_agent(settings, AskRequest(question="最近7天的每日负荷趋势"), model=model)
    assert response["outcome"] == "success"
    assert response["semantic_check"]["status"] == "verified"
    assert response["usage"]["calls"] == 3
    assert len(response["result"]["rows"]) == 7
    assert response["stop_reason"] == "finished"
    assert response["trace"][-1]["node"] == "finish_analysis"
    metric_step = next(t for t in response["trace"] if t["node"] == "calculate_metric")
    assert any(t["node"] == "execute" for t in metric_step["execution_steps"])
    contract = json.loads(model.inputs[0][0]["content"])["task_contract"]
    assert set(contract["required_columns"]) == {
        "day",
        "load_mw",
        "forecast_mw",
        "observed_hours",
        "forecast_hours",
    }
    assert contract["supported_metric_tool"] == "calculate_metric"
    assert "SELECT" not in json.dumps(contract)
    observation = json.loads(model.inputs[-1][-1]["output"])
    assert observation["preview_rows"] == response["result"]["rows"]
    assert observation["evidence_id"] == "e1"


def test_model_recovers_from_actual_sql_error(settings):
    bad = compile_task(parse_task("记录数")).model_dump()
    bad["sql"] = "SELECT missing_column FROM load_hourly"
    good = compile_task(parse_task("记录数")).model_dump()
    model = ScriptedModel(
        [("query_load", bad), ("query_load", good), ("finish_analysis", {"evidence_id": "e1"})]
    )
    response = ask_tool_agent(settings, AskRequest(question="记录数"), model=model)
    assert response["result"]["rows"] == [{"n": 168}]
    assert "missing_column" in json.loads(model.inputs[1][-1]["output"])["error"]
    assert response["trace"][0]["status"] == "error"


def test_semantic_failure_cannot_be_finished_as_evidence(settings):
    bad = compile_task(parse_task("整体负荷趋势")).model_dump()
    bad["sql"] = bad["sql"].replace("AVG(load_mw)", "SUM(load_mw)")
    model = ScriptedModel(
        [
            ("query_load", bad),
            ("finish_analysis", {"evidence_id": "e1"}),
            ("finish_analysis", {"evidence_id": "e1"}),
        ]
    )
    response = ask_tool_agent(settings, AskRequest(question="整体负荷趋势"), model=model)
    assert response["outcome"] == "failed"
    assert response["error_code"] == "budget_exhausted"
    assert response["result"]["rows"] == []
    assert json.loads(model.inputs[1][-1]["output"])["error_code"] == "semantic_error"


def test_budget_does_not_publish_unselected_partial_results(settings):
    model = ScriptedModel([("calculate_metric", {})])
    response = ask_tool_agent(
        replace(settings, max_model_calls=1), AskRequest(question="记录数"), model=model
    )
    assert model.usage["calls"] == 1
    assert response["result"]["rows"] == []
    assert response["error_code"] == "budget_exhausted"


@pytest.mark.parametrize("question", ["删除所有记录", "查询电价", "2027-01-01的负荷趋势"])
def test_preflight_rejection_has_no_model_calls(settings, question):
    model = ScriptedModel([])
    response = ask_tool_agent(settings, AskRequest(question=question), model=model)
    assert response["outcome"] in ("rejected", "refused")
    assert model.usage["calls"] == 0


def test_plain_prose_is_not_a_verified_answer(settings):
    model = ScriptedModel([])
    model.respond = lambda *args: [{"type": "message", "content": "The demand was 42."}]
    response = ask_tool_agent(settings, AskRequest(question="负荷趋势"), model=model)
    assert response["error_code"] == "missing_evidence"
    assert response["chart"] is None


def test_anomaly_budget_failure_stays_a_failure(settings):
    model = ScriptedModel([("inspect_dataset", {})])
    response = ask_tool_agent(
        replace(settings, max_model_calls=1), AskRequest(question="异常筛查"), model=model
    )
    assert response["error_code"] == "budget_exhausted"
    assert response["result"]["rows"] == []


def test_plot_and_evidence_ids_are_run_local(settings):
    runtime = ToolRuntime(settings, AskRequest(question="负荷趋势"), "domain")
    assert runtime.call("finish_analysis", {"evidence_id": "e1"})["ok"] is False
    assert runtime.call("calculate_metric", {})["evidence_id"] == "e1"
    assert (
        runtime.call("plot_result", {"evidence_id": "e1", "chart": "line", "x": "day", "y": ["invented"]})[
            "ok"
        ]
        is False
    )
    assert (
        runtime.call("plot_result", {"evidence_id": "e1", "chart": "bar", "x": "day", "y": ["load_mw"]})["ok"]
        is True
    )
    assert runtime.evidence["e1"]["chart"]["type"] == "bar"


def test_custom_task_cannot_silently_use_a_different_metric(settings):
    runtime = ToolRuntime(settings, AskRequest(question="负荷中位数"), "domain")
    assert runtime.call("calculate_metric", {})["ok"] is False
    assert runtime.call("run_shell", {"cmd": "anything"})["ok"] is False


def test_real_sdk_tool_call_protocol_and_no_hidden_retries(settings, monkeypatch):
    payloads = []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        name, arguments = (
            ("calculate_metric", {}) if len(payloads) == 1 else ("finish_analysis", {"evidence_id": "e1"})
        )
        assert payload["store"] is False
        assert payload["parallel_tool_calls"] is False
        assert all(t["strict"] for t in payload["tools"])
        assert "unit-test-placeholder" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "id": f"resp_{len(payloads)}",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "test-model",
                "output": [
                    {
                        "type": "function_call",
                        "id": f"fc_{len(payloads)}",
                        "call_id": f"call_{len(payloads)}",
                        "name": name,
                        "arguments": json.dumps(arguments),
                        "status": "completed",
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120},
            },
        )

    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    client = OpenAI(
        api_key="unit-test-placeholder",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr("energy_agent.tool_agent.OpenAI", lambda **kwargs: client)
    try:
        result = ask_tool_agent(replace(settings, model="test-model"), AskRequest(question="记录数"))
        assert result["usage"]["calls"] == 2
        assert result["usage"]["input_tokens"] == 200
        assert payloads[1]["input"][-1]["type"] == "function_call_output"
        assert json.loads(payloads[1]["input"][-1]["output"])["returned_rows"] == 1
    finally:
        client.close()


def test_provider_error_uses_one_attempt_and_redacts_body(settings, monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(429, json={"error": {"message": "private diagnostic"}})

    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    client = OpenAI(
        api_key="unit-test-placeholder",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    monkeypatch.setattr("energy_agent.tool_agent.OpenAI", lambda **kwargs: client)
    result = ask_tool_agent(settings, AskRequest(question="记录数"))
    assert attempts == [1]
    assert result["error_code"] == "provider_error"
    assert "private diagnostic" not in result["answer"]
    assert result["usage"]["calls"] == 1


def test_missing_key_fails_before_network(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        ResponsesToolModel("test-model")
