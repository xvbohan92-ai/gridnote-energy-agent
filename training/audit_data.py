import json

from common import ROOT, NoThinkingTokenizer, read_rows, sha, verify_frozen
from mlx_lm.tuner.datasets import ChatDataset
from mlx_lm.utils import load_tokenizer

from energy_agent.data import dataset_context
from energy_agent.models import QueryPlan, Scope
from energy_agent.real_validation import expected_rows, matches, source_oracle
from energy_agent.sql import execute_sql, guard_sql


def audit():
    config = json.loads((ROOT / "training/config.json").read_text())
    manifest = verify_frozen()
    tokenizer = NoThinkingTokenizer(load_tokenizer(ROOT / config["model"], {"trust_remote_code": False}))
    context = dataset_context(ROOT / "data/eia-pjm.sqlite")
    raw = source_oracle(ROOT / "data/sources", context["metadata"]["sources"], 2024)
    summary = {}
    for split in manifest["splits"]:
        rows = read_rows(split)
        dataset = ChatDataset(rows, tokenizer, mask_prompt=True)
        lengths = []
        for row in rows:
            encoded, offset = dataset.process(row)
            prefix = tokenizer.apply_chat_template(
                row["messages"][:-1], add_generation_prompt=True, return_dict=False
            )
            assert encoded[:offset] == prefix, f"Completion mask mismatch: {row['id']}"
            assert 0 < offset < len(encoded) <= config["max_seq_length"], row["id"]
            plan = QueryPlan.model_validate_json(row["messages"][-1]["content"])
            sql = guard_sql(plan.sql, Scope.model_validate(row["scope"]))
            result = execute_sql(ROOT / "data/eia-pjm.sqlite", sql)
            gold = expected_rows(raw, {"start": row["start"], "end": row["end"], **row["task"]})
            assert matches(gold, result["rows"]), f"Reference mismatch: {row['id']}"
            lengths.append({"total": len(encoded), "completion": len(encoded) - offset})
        summary[split] = {
            "rows": len(rows),
            "gold_execution_checks": len(rows),
            "prefix_masks_checked": len(rows),
            "max_tokens": max(r["total"] for r in lengths),
            "mean_completion_tokens": sum(r["completion"] for r in lengths) / len(lengths),
        }
    report = {
        "dataset": manifest,
        "tokenization": summary,
        "training_config_sha256": sha(ROOT / "training/config.json"),
        "oracle": "Separate EIA CSV decoder and metric scorer; no model-generated test outputs.",
        "truncation": False,
        "thinking": False,
        "prompt_loss_masked": True,
    }
    (ROOT / "reports/sft-data-audit.json").write_text(json.dumps(report, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    return report


if __name__ == "__main__":
    audit()
