import json
import shutil

import pytest

from energy_agent.models import QueryPlan
from training import common
from training.prepare_data import ROOT, build


def test_sft_conditions_and_phrase_families_are_disjoint(tmp_path):
    report = build(tmp_path)
    assert {k: v["rows"] for k, v in report["splits"].items()} == {
        "train": 232,
        "valid": 24,
        "test": 32,
    }
    rows = {
        split: [json.loads(s) for s in (tmp_path / f"{split}.jsonl").read_text().splitlines()]
        for split in report["splits"]
    }
    for left, right in (("train", "valid"), ("train", "test"), ("valid", "test")):
        assert not {tuple(r["condition"]) for r in rows[left]} & {tuple(r["condition"]) for r in rows[right]}
        assert not {r["phrase_family"] for r in rows[left]} & {r["phrase_family"] for r in rows[right]}
    assert max(r["end"] for r in rows["train"]) < min(r["start"] for r in rows["valid"])
    assert max(r["end"] for r in rows["valid"]) < min(r["start"] for r in rows["test"])


def test_prompt_contains_contract_but_not_gold_sql(tmp_path):
    build(tmp_path)
    for path in tmp_path.glob("*.jsonl"):
        for line in path.read_text().splitlines():
            row = json.loads(line)
            target = QueryPlan.model_validate_json(row["messages"][-1]["content"])
            request = json.loads(row["messages"][1]["content"])
            assert "required_columns" in request["task_contract"]
            if row["mode"] == "direct":
                assert request["previous_attempt"] is None
                assert target.sql not in row["messages"][1]["content"]
            else:
                assert request["previous_attempt"]["sql"] != target.sql


def test_frozen_dataset_rejects_later_mutation(tmp_path, monkeypatch):
    output = tmp_path / "training/datasets"
    build(output)
    (tmp_path / "energy_agent").mkdir()
    shutil.copyfile(ROOT / "prepare_data.py", tmp_path / "training/prepare_data.py")
    shutil.copyfile(ROOT.parent / "energy_agent/sql_sft.py", tmp_path / "energy_agent/sql_sft.py")
    monkeypatch.setattr(common, "ROOT", tmp_path)
    assert common.verify_frozen()["splits"]["test"]["rows"] == 32
    with (output / "test.jsonl").open("a") as stream:
        stream.write("{}\n")
    with pytest.raises(ValueError, match="Frozen test"):
        common.verify_frozen()
