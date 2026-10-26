from dataclasses import replace

import httpx
import pytest

from energy_agent.agent import ask
from energy_agent.errors import PlanningError
from energy_agent.models import AskRequest
from energy_agent.planner import MLXPlanner
from energy_agent.tasks import compile_task, parse_task


def test_trained_sql_flows_through_existing_execution_and_verifier(settings, monkeypatch):
    plan = compile_task(parse_task("负荷记录数")).model_dump()

    def handler(request):
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/plan"
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            json={
                "plan": plan,
                "model": "test trained model",
                "usage": {"input_tokens": 100, "output_tokens": 30},
            },
        )

    client = httpx.Client(base_url="http://127.0.0.1:8768", transport=httpx.MockTransport(handler))
    monkeypatch.setattr("energy_agent.planner.httpx.Client", lambda **kwargs: client)
    response = ask(replace(settings, planner="mlx-sql"), AskRequest(question="负荷记录数"))
    assert response["provider"] == "mlx"
    assert response["usage"]["calls"] == 1
    assert response["model"] == "test trained model"
    assert response["semantic_check"]["status"] == "verified"
    assert response["result"]["rows"] == [{"n": 168}]


def test_local_model_failure_is_not_a_successful_refusal(settings, monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503, json={"detail": "private internal diagnostic"})

    client = httpx.Client(base_url="http://127.0.0.1:8768", transport=httpx.MockTransport(handler))
    monkeypatch.setattr("energy_agent.planner.httpx.Client", lambda **kwargs: client)
    response = ask(replace(settings, planner="mlx-sql"), AskRequest(question="负荷记录数"))
    assert calls == [1]
    assert response["outcome"] == "failed"
    assert response["error_code"] == "provider_error"
    assert "private internal diagnostic" not in response["answer"]


def test_mlx_never_routes_to_remote_service():
    with pytest.raises(PlanningError, match="本机"):
        MLXPlanner("https://example.com", "test")
