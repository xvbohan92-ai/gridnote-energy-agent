import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from energy_agent.agent import ask
from energy_agent.api import create_app
from energy_agent.config import Settings
from energy_agent.errors import PlanningError
from energy_agent.models import AskRequest
from energy_agent.tool_agent import OllamaToolModel


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/v1",
        "http://localhost:11434/v1?key=bad",
        "http://user:password@localhost:11434/v1",
        "http://127.0.0.1:11434/other",
    ],
)
def test_local_provider_rejects_remote_or_credential_urls(url):
    with pytest.raises(PlanningError, match="本机"):
        OllamaToolModel("qwen3:4b", url)


def test_local_provider_rejects_cloud_models():
    with pytest.raises(PlanningError, match="云端"):
        OllamaToolModel("qwen3:cloud", "http://127.0.0.1:11434/v1")


def test_local_action_protocol_never_uses_cloud_key_or_cloud_prices(settings, monkeypatch):
    payloads, constructor = [], {}

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        assert request.url.host == "127.0.0.1"
        assert request.url.path == "/api/chat"
        assert "authorization" not in request.headers
        assert payload["model"] == "qwen3:4b"
        assert payload["think"] is False
        assert len(payload["format"]["oneOf"]) == 5
        assert "never-send-this-cloud-key" not in request.content.decode()
        name, args = (
            ("calculate_metric", {}) if len(payloads) == 1 else ("finish_analysis", {"evidence_id": "e1"})
        )
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": json.dumps({"name": name, "arguments": args})},
                "prompt_eval_count": 100,
                "eval_count": 20,
                "done": True,
            },
        )

    client = httpx.Client(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handler))

    def substitute(**kwargs):
        constructor.update(kwargs)
        return client

    monkeypatch.setattr("energy_agent.tool_agent.httpx.Client", substitute)
    monkeypatch.setenv("OPENAI_API_KEY", "never-send-this-cloud-key")
    monkeypatch.setenv("OPENAI_INPUT_USD_PER_MILLION", "9999")
    monkeypatch.setenv("OPENAI_OUTPUT_USD_PER_MILLION", "9999")
    response = ask(
        replace(settings, planner="ollama-tools", model="qwen3:4b"), AskRequest(question="负荷记录数")
    )
    assert constructor["trust_env"] is False
    assert constructor["follow_redirects"] is False
    assert response["provider"] == "ollama"
    assert response["engine"] == "ollama-json-action-loop"
    assert response["result"]["rows"] == [{"n": 168}]
    assert response["usage"]["calls"] == 2
    assert response["usage"]["estimated_cost_usd"] == 0.0


def test_environment_uses_separate_local_model_setting(monkeypatch):
    monkeypatch.setenv("ENERGY_PLANNER", "ollama-tools")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen3:4b")
    monkeypatch.setenv("OPENAI_MODEL", "different-cloud-model")
    assert Settings.from_env().model == "qwen3:4b"


def test_local_preview_and_policy_refusal_do_not_run_model(settings, monkeypatch):
    class ForbiddenModel:
        def __init__(self, *args, **kwargs):
            raise AssertionError("A preview or preflight refusal must not run inference")

    monkeypatch.setattr("energy_agent.tool_agent.OllamaToolModel", ForbiddenModel)
    configured = replace(settings, planner="ollama-tools", model="qwen3:4b")
    with TestClient(create_app(configured)) as client:
        health = client.get("/api/health").json()
        assert health["provider"] == "ollama"
        assert health["model"] == "qwen3:4b"
        assert health["model_configured"] is True
        assert client.get("/api/preview").json()["usage"]["calls"] == 0
        result = client.post("/api/ask", json={"question": "查询电价"}).json()
        assert result["outcome"] == "refused"
        assert result["usage"]["calls"] == 0


@pytest.mark.parametrize(
    "status,content,expected",
    [
        (200, '{"name":"run_shell","arguments":{}}', "invalid_plan"),
        (503, "private local diagnostic", "provider_error"),
    ],
)
def test_local_failures_stop_without_retry_or_unregistered_actions(
    settings, monkeypatch, status, content, expected
):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, json={"message": {"content": content}})

    client = httpx.Client(base_url="http://127.0.0.1:11434", transport=httpx.MockTransport(handler))
    monkeypatch.setattr("energy_agent.tool_agent.httpx.Client", lambda **kwargs: client)
    response = ask(
        replace(settings, planner="ollama-tools", model="qwen3:4b"), AskRequest(question="负荷记录数")
    )
    assert calls == [1]
    assert response["error_code"] == expected
    assert response["result"]["rows"] == []
    assert "private local diagnostic" not in response["answer"]
