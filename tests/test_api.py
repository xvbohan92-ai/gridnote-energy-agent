from fastapi.testclient import TestClient

from energy_agent.api import create_app


def test_http_workflow_returns_evidence_and_chart(settings):
    with TestClient(create_app(settings)) as client:
        assert client.get("/").status_code == 200
        assert "default-src 'self'" in client.get("/").headers["content-security-policy"]
        assert client.get("/static/app.js").status_code == 200
        context = client.get("/api/dataset").json()
        assert context["metadata"]["synthetic"] is True
        response = client.post("/api/ask", json={"question": "最近7天负荷趋势", "region": "DOM"})
        result = response.json()
        assert response.status_code == 200
        assert result["status"] == "ok"
        assert len(result["result"]["rows"]) == 7
        assert result["source"]["synthetic"] is True
        assert result["chart"]["x"] == "day"
        assert result["semantic_check"]["status"] == "verified"
        assert result["pipeline"] == "domain"
        assert result["task"]["granularity"] == "day"
        assert result["quality"]["observed_hours"] == 168
        assert "SELECT" in result["sql"]
        assert client.post("/api/ask", json={"question": "x"}).status_code == 422
        assert client.post("/api/ask", json={"question": "负荷", "planner": "unsafe"}).status_code == 422


def test_preview_is_free_even_when_openai_mode_is_selected(settings, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    configured = settings.__class__(settings.db_path, planner="openai")
    with TestClient(create_app(configured)) as client:
        result = client.get("/api/preview").json()
        assert result["planner"] == "demo"
        assert result["usage"]["calls"] == 0
        response = client.post("/api/ask", json={"question": "负荷趋势"})
        assert response.status_code == 400
        assert "OPENAI_API_KEY" in response.json()["detail"]
