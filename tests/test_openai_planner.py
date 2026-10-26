"""Exercise the real SDK's HTTP/structured-output contract without network or credentials."""

import json

import httpx
import pytest
from openai import OpenAI

from energy_agent.models import Scope
from energy_agent.planner import OpenAIPlanner, PlanningError


def configure_client(monkeypatch, handler):
    monkeypatch.setenv("OPENAI_API_KEY", "unit-test-placeholder")
    monkeypatch.delenv("OPENAI_INPUT_USD_PER_MILLION", raising=False)
    monkeypatch.delenv("OPENAI_OUTPUT_USD_PER_MILLION", raising=False)
    client = OpenAI(
        api_key="unit-test-placeholder", http_client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    monkeypatch.setattr("energy_agent.planner.OpenAI", lambda **kwargs: client)
    return client


def test_sdk_parses_structured_plan_and_tracks_usage(monkeypatch):
    plan = {
        "title": "记录数",
        "sql": "SELECT COUNT(*) AS n FROM load_hourly",
        "chart": "none",
        "x": "",
        "y": [],
        "note": "按指定范围统计",
    }

    def handler(request):
        payload = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert payload["store"] is False
        assert payload["text"]["format"]["type"] == "json_schema"
        assert payload["text"]["format"]["strict"] is True
        assert payload["model"] == "test-model"
        assert "unit-test-placeholder" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "status": "completed",
                "model": "test-model",
                "output": [
                    {
                        "type": "message",
                        "id": "msg_test",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": json.dumps(plan), "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 200, "output_tokens": 40, "total_tokens": 240},
            },
        )

    client = configure_client(monkeypatch, handler)
    try:
        planner = OpenAIPlanner("test-model")
        result = planner.plan(
            "负荷记录数",
            {"schema": {"load_hourly": {}}},
            Scope(start="2025-01-01T00:00:00Z", end="2025-01-02T00:00:00Z", region="DOM"),
        )
        assert result.sql == plan["sql"]
        assert planner.usage["calls"] == 1
        assert planner.usage["input_tokens"] == 200
        assert planner.usage["output_tokens"] == 40
        assert planner.usage["estimated_cost_usd"] is None
    finally:
        client.close()


def test_provider_errors_never_echo_response_body(monkeypatch):
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "private upstream diagnostic"}})

    client = configure_client(monkeypatch, handler)
    try:
        with pytest.raises(PlanningError) as error:
            OpenAIPlanner("test-model").plan(
                "负荷趋势", {"schema": {}}, Scope(start="2025-01-01", end="2025-01-02", region="DOM")
            )
        assert "private upstream diagnostic" not in str(error.value)
        assert "AuthenticationError" in str(error.value)
    finally:
        client.close()
