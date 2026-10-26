import json
import sys
from dataclasses import replace

import pytest

from energy_agent.cli import main


@pytest.mark.parametrize("planner", ["openai-tools", "ollama-tools", "mlx-sql"])
def test_batch_evaluation_cannot_inherit_paid_tool_loop_from_environment(
    settings, monkeypatch, tmp_path, planner
):
    configured = replace(settings, planner=planner)
    monkeypatch.setattr("energy_agent.cli.Settings.from_env", lambda: configured)
    report = tmp_path / "evaluation.json"
    monkeypatch.setattr(sys, "argv", ["energy-agent", "evaluate", "--output", str(report)])

    class ForbiddenModel:
        def __init__(self, *args, **kwargs):
            raise AssertionError("Batch validation unexpectedly attempted to construct a paid model")

    monkeypatch.setattr("energy_agent.tool_agent.ResponsesToolModel", ForbiddenModel)
    monkeypatch.setattr("energy_agent.tool_agent.OllamaToolModel", ForbiddenModel)
    monkeypatch.setattr("energy_agent.agent.OpenAIPlanner", ForbiddenModel)
    monkeypatch.setattr("energy_agent.agent.MLXPlanner", ForbiddenModel)
    main()
    result = json.loads(report.read_text())
    assert result["planner"] == "demo"
    assert all(s["model_calls"] == 0 for s in result["summary"].values())
