import hashlib
import json

from energy_agent import api


def test_training_evidence_requires_matching_source_reports(tmp_path, monkeypatch):
    reports = tmp_path / "reports"
    figures = tmp_path / "docs/figures"
    reports.mkdir()
    figures.mkdir(parents=True)
    trained = {
        "config": {"iters": 160, "lora_parameters": {"rank": 8}},
        "best_checkpoint": {"iteration": 119},
        "trainable_parameters": 3670016,
        "peak_memory_bytes": 3703982362,
    }
    evaluated = {
        "summary": {
            "base": {"cases": 32, "execution_correct": 7},
            "qlora": {"cases": 32, "execution_correct": 22},
        },
        "paired": {},
        "dataset": {"splits": {}},
    }
    train_bytes = json.dumps(trained).encode()
    evaluation_bytes = json.dumps(evaluated).encode()
    (reports / "sft-training.json").write_bytes(train_bytes)
    (reports / "sft-evaluation.json").write_bytes(evaluation_bytes)
    (figures / "sft-chart-values.json").write_text(
        json.dumps(
            {
                "training_report_sha256": hashlib.sha256(train_bytes).hexdigest(),
                "evaluation_report_sha256": hashlib.sha256(evaluation_bytes).hexdigest(),
            }
        )
    )
    monkeypatch.setattr(api, "ROOT", tmp_path)
    assert api.training_evidence()["qlora"]["execution_correct"] == 22
    (reports / "sft-evaluation.json").write_text("changed artifact")
    assert api.training_evidence() == {"available": False}


def test_missing_training_artifacts_do_not_break_the_app(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "ROOT", tmp_path)
    assert api.training_evidence() == {"available": False}
