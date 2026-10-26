"""One frozen paired test evaluation after validation-only checkpoint selection."""

import importlib.metadata
import json
import statistics
import time

import mlx.core as mx
from common import ROOT, read_rows, sha, verify_frozen
from inference import generate_plan
from mlx.utils import tree_flatten
from mlx_lm import load
from mlx_lm.tuner.utils import load_adapters
from pydantic import ValidationError

from energy_agent.data import dataset_context
from energy_agent.models import QueryPlan, Scope
from energy_agent.real_validation import expected_rows, matches, source_oracle
from energy_agent.sql import QueryError, execute_sql, guard_sql


def score(text, row, raw):
    result = {
        "format_valid": False,
        "sql_guard_pass": False,
        "executed": False,
        "execution_correct": False,
        "chart_valid": False,
        "sql": None,
        "failure": None,
    }
    try:
        plan = QueryPlan.model_validate_json(text.strip())
        result.update(format_valid=True, sql=plan.sql)
        sql = guard_sql(plan.sql, Scope.model_validate(row["scope"]))
        result["sql_guard_pass"] = True
        output = execute_sql(ROOT / "data/eia-pjm.sqlite", sql)
        result.update(executed=True, returned_rows=len(output["rows"]))
        gold = expected_rows(raw, {"start": row["start"], "end": row["end"], **row["task"]})
        result["execution_correct"] = matches(gold, output["rows"])
        result["chart_valid"] = plan.chart == "none" or (
            plan.x in output["columns"]
            and bool(plan.y)
            and all(
                y in output["columns"] and any(isinstance(r.get(y), (int, float)) for r in output["rows"])
                for y in plan.y
            )
        )
        if not result["execution_correct"]:
            result["failure"] = "oracle_mismatch"
    except ValidationError:
        result["failure"] = "invalid_json_plan"
    except QueryError as exc:
        result["failure"] = str(exc)[:250]
    return result


def summarize(records):
    return {
        "cases": len(records),
        **{
            key: sum(r[key] for r in records)
            for key in ("format_valid", "sql_guard_pass", "executed", "execution_correct", "chart_valid")
        },
        "execution_accuracy": sum(r["execution_correct"] for r in records) / len(records),
        "median_latency_ms": statistics.median(r["latency_ms"] for r in records),
        "input_tokens": sum(r["input_tokens"] for r in records),
        "output_tokens": sum(r["output_tokens"] for r in records),
    }


def main():
    manifest = verify_frozen()
    trained = json.loads((ROOT / "reports/sft-training.json").read_text())
    config = trained["config"]
    assert trained["status"] == "trained"
    assert sha(ROOT / "training/config.json") == trained["config_sha256"]
    assert manifest == trained["dataset"]
    assert sha(ROOT / config["best_path"] / "adapters.safetensors") == trained["selected_adapter_sha256"]
    output = ROOT / "reports/sft-evaluation.json"
    if output.exists():
        raise ValueError("A frozen evaluation already exists; retain it rather than silently rerunning tests")
    mx.random.seed(config["seed"])
    model, tokenizer = load(
        ROOT / config["model"], tokenizer_config={"trust_remote_code": False}, trust_remote_code=False
    )
    context = dataset_context(ROOT / "data/eia-pjm.sqlite")
    raw = source_oracle(ROOT / "data/sources", context["metadata"]["sources"], 2024)
    test = read_rows("test")
    records, summaries = [], {}
    raw_path = ROOT / "data/finetuning/sql-qlora-v1/evaluation-raw.jsonl"
    started = time.perf_counter()
    for phase in ("base", "qlora"):
        if phase == "qlora":
            model = load_adapters(model, str(ROOT / config["best_path"]))
            model.eval()
            loaded = dict(tree_flatten(model.parameters()))
            expected = mx.load(str(ROOT / config["best_path"] / "adapters.safetensors"))
            assert all(k in loaded and bool(mx.all(loaded[k] == v)) for k, v in expected.items())
        # Same validation warm-up in both modes; no test prompt cache is shared.
        generate_plan(model, tokenizer, read_rows("valid")[0]["messages"][:-1], config)
        for index, row in enumerate(test):
            text, timing = generate_plan(model, tokenizer, row["messages"][:-1], config)
            record = {
                "id": row["id"],
                "phase": phase,
                "condition": row["condition"],
                "mode": row["mode"],
                "question": row["question"],
                **timing,
                **score(text, row, raw),
            }
            records.append(record)
            with raw_path.open("a") as stream:
                stream.write(
                    json.dumps({"id": row["id"], "phase": phase, "output": text}, ensure_ascii=False) + "\n"
                )
            print(
                json.dumps(
                    {
                        "phase": phase,
                        "case": index + 1,
                        "total": len(test),
                        "correct": record["execution_correct"],
                        "failure": record["failure"],
                    }
                ),
                flush=True,
            )
        summaries[phase] = summarize([r for r in records if r["phase"] == phase])
    base = {r["id"]: r for r in records if r["phase"] == "base"}
    tuned = {r["id"]: r for r in records if r["phase"] == "qlora"}
    paired = {
        "improved": sum(not base[k]["execution_correct"] and tuned[k]["execution_correct"] for k in base),
        "regressed": sum(base[k]["execution_correct"] and not tuned[k]["execution_correct"] for k in base),
        "both_correct": sum(base[k]["execution_correct"] and tuned[k]["execution_correct"] for k in base),
        "both_wrong": sum(
            not base[k]["execution_correct"] and not tuned[k]["execution_correct"] for k in base
        ),
    }
    by_mode = {
        phase: {
            mode: summarize([r for r in records if r["phase"] == phase and r["mode"] == mode])
            for mode in ("direct", "repair")
        }
        for phase in ("base", "qlora")
    }
    report = {
        "experiment": manifest["experiment"],
        "dataset": manifest,
        "evaluation_code_sha256": sha(__file__),
        "generation_code_sha256": sha(ROOT / "training/inference.py"),
        "sql_guard_code_sha256": sha(ROOT / "energy_agent/sql.py"),
        "training_report_sha256": sha(ROOT / "reports/sft-training.json"),
        "selected_adapter_sha256": trained["selected_adapter_sha256"],
        "model_repo": config["model_repo"],
        "model_revision": config["model_revision"],
        "inference": {
            "same_base_weights": True,
            "thinking": False,
            "temperature": config["generation_temperature"],
            "max_tokens": config["generation_max_tokens"],
            "json_grammar": False,
            "model_load_time_excluded": True,
            "warmup": "one validation example each",
            "ordering": "base then selected QLoRA; no prompt cache reuse",
        },
        "versions": {name: importlib.metadata.version(name) for name in ("mlx", "mlx-lm", "sqlglot")},
        "summary": summaries,
        "paired": paired,
        "by_mode": by_mode,
        "records": records,
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "limitations": "32 programmatically authored cases from 8 held-out semantic conditions; "
        "paraphrases are correlated. Supplied correct finite task contract and one schema, shared "
        "metric primitives; no external human review or general text-to-SQL claim. "
        "Repair faults are synthetic. Execution scores use separate raw EIA CSV calculations. "
        "No tool retries, no trusted-calculator fallback and no per-phase prompt/decoding changes. "
        "Latency is an individual local warm run with base-first order, not a production SLA.",
    }
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"summary": summaries, "paired": paired}, indent=2), flush=True)


if __name__ == "__main__":
    main()
