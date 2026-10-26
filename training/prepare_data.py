"""Programmatic SFT pilot. Semantic conditions and phrase families are split as groups."""

import hashlib
import json
from pathlib import Path

from energy_agent.models import AnalysisTask, Scope
from energy_agent.sql_sft import messages
from energy_agent.tasks import compile_task

ROOT = Path(__file__).resolve().parent
TEST = {
    ("trend", "week", "weekday"),
    ("trend", "total", "weekend"),
    ("trend", "day_type", "all"),
    ("energy", "day", "weekday"),
    ("energy", "week", "weekend"),
    ("forecast", "week", "weekday"),
    ("forecast", "total", "weekend"),
    ("peak", "hour", "weekend"),
}
VALID = {
    ("trend", "hour", "weekday"),
    ("trend", "week", "weekend"),
    ("energy", "hour", "weekend"),
    ("energy", "total", "weekday"),
    ("energy", "day_type", "all"),
    ("forecast", "hour", "weekday"),
    ("forecast", "day", "weekend"),
    ("count", "total", "weekend"),
}
WRAPPERS = {
    "train": [
        "Please return {metric}.",
        "请查询{metric}。",
        "Show {metric}.",
        "我需要{metric}。",
        "Calculate {metric} for my review.",
        "生成{metric}的结果。",
    ],
    "valid": ["Prepare an audit table of {metric}.", "请为核对生成{metric}的明细。"],
    "test": [
        "For a retrospective report, provide {metric}.",
        "为了复盘，请列出{metric}。",
        "The requested evidence is {metric}; return its data.",
    ],
}
WINDOWS = {
    "train": [("2024-04-01", "2024-04-14"), ("2024-05-06", "2024-05-19")],
    "valid": [("2024-07-01", "2024-07-14"), ("2024-08-05", "2024-08-18")],
    "test": [("2024-11-01", "2024-11-14"), ("2024-12-02", "2024-12-15")],
}
HOUR_WINDOWS = {
    "train": ("2024-04-05", "2024-04-07"),
    "valid": ("2024-07-05", "2024-07-07"),
    "test": ("2024-11-01", "2024-11-03"),
}


def conditions():
    result = [
        (kind, granularity, day_filter)
        for kind in ("trend", "energy", "forecast")
        for granularity in ("hour", "day", "week", "total")
        for day_filter in ("all", "weekday", "weekend")
    ]
    result += [(kind, "day_type", "all") for kind in ("trend", "energy", "forecast")]
    result += [
        (kind, "hour" if kind == "peak" else "total", day_filter)
        for kind in ("count", "peak")
        for day_filter in ("all", "weekday", "weekend")
    ]
    return sorted(result)


def metric(task, chinese):
    units = {
        "hour": ("每小时", "hourly"),
        "day": ("每日", "daily"),
        "week": ("每周", "weekly"),
        "total": ("整体", "overall"),
        "day_type": ("工作日与周末分别", "separate weekday and weekend"),
    }
    periods = units[task.granularity][0 if chinese else 1]
    filters = {
        "all": ("", ""),
        "weekday": ("仅工作日的", "weekday-only "),
        "weekend": ("仅周末的", "weekend-only "),
    }[task.day_filter][0 if chinese else 1]
    names = {
        "trend": ("平均负荷趋势", "mean actual and forecast load"),
        "energy": ("观测用电量 MWh", "observed energy in MWh"),
        "forecast": ("预测误差 MAE MSE MAPE 与分母", "forecast MAE, MSE, MAPE and denominators"),
        "count": ("负荷记录数", "count of load records"),
        "peak": (f"负荷最高的{task.top_k}个小时", f"top {task.top_k} load hours"),
    }
    return filters + periods + ("" if chinese else " ") + names[task.kind][0 if chinese else 1]


def make_case(split, combo, variant, *, repair=False):
    kind, granularity, day_filter = combo
    task = AnalysisTask(
        kind=kind,
        granularity=granularity,
        day_filter=day_filter,
        top_k=(3, 5, 10)[variant % 3] if kind == "peak" else 5,
        unit="MWh" if kind == "energy" else "rows" if kind == "count" else "MW",
    )
    start, end = HOUR_WINDOWS[split] if granularity == "hour" else WINDOWS[split][variant % 2]
    # Scope note carries no gold SQL or expected result values.
    scope = Scope(start=start + "T00:00:00Z", end=end + "T23:00:00Z", region="PJM")
    # end is a strict exclusive boundary at the next UTC midnight.
    from datetime import datetime, timedelta

    scope.end = (datetime.fromisoformat(end) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    wrapper = WRAPPERS[split][variant % len(WRAPPERS[split])]
    chinese = any("\u4e00" <= c <= "\u9fff" for c in wrapper)
    question = wrapper.format(metric=metric(task, chinese))
    plan = compile_task(task).model_copy(update={"note": "UTC；按任务定义计算，缺失值不补零。"})
    previous = None
    if repair:
        if kind == "peak":
            wrong = plan.sql.replace("DESC", "ASC")
            error = "semantic_error: peak order must be load_mw DESC, timestamp_utc ASC."
        elif kind == "count":
            wrong = plan.sql.replace("COUNT(*)", "COUNT(forecast_mw)")
            error = "semantic_error: count actual records, including rows with missing forecasts."
        elif variant % 2:
            wrong = (
                plan.sql.replace("AVG(load_mw)", "SUM(load_mw)")
                if kind == "trend"
                else (
                    plan.sql.replace("SUM(load_mw)", "AVG(load_mw)")
                    if kind == "energy"
                    else plan.sql.replace("forecast_mw", "COALESCE(forecast_mw, 0)")
                )
            )
            error = "semantic_error: obey power/energy aggregation and do not replace NULL forecasts by zero."
        else:
            wrong = plan.sql.replace("observed_hours", "records_seen")
            error = "semantic_error: return the required column aliases from the task contract."
        previous = {"sql": wrong, "error": error}
    conversation = messages(question, scope, task, previous)
    conversation.append({"role": "assistant", "content": plan.model_dump_json()})
    return {
        "id": f"{split}-{'-'.join(combo)}-{variant}-{'repair' if repair else 'direct'}",
        "split": split,
        "condition": list(combo),
        "mode": "repair" if repair else "direct",
        "phrase_family": f"{split}-{variant % len(WRAPPERS[split])}",
        "question": question,
        "start": start,
        "end": end,
        "scope": scope.model_dump(),
        "task": task.model_dump(),
        "label_provenance": "programmatic trusted finite-task compiler",
        "messages": conversation,
    }


def build(output=ROOT / "datasets"):
    output.mkdir(parents=True, exist_ok=True)
    splits = {name: [] for name in WRAPPERS}
    for combo in conditions():
        split = "test" if combo in TEST else "valid" if combo in VALID else "train"
        variants = range(len(WRAPPERS[split]))
        for variant in variants:
            splits[split].append(make_case(split, combo, variant))
        for variant in range(2 if split == "train" else 1):
            splits[split].append(make_case(split, combo, variant, repair=True))
    manifest = {
        "experiment": "gridnote-contract-sql-qlora-v1",
        "splits": {},
        "limitations": "Programmatically authored synthetic instructions/SQL labels, not human "
        "annotations. Same schema and metric primitives across splits. Conditions and phrase "
        "families are grouped; real EIA data is used only for execution scoring. "
        "This evaluates a supplied correct task contract, not general intent recognition.",
    }
    prompt_sets, condition_sets = {}, {}
    for split, rows in splits.items():
        contents = "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in rows)
        path = output / f"{split}.jsonl"
        path.write_text(contents)
        prompts = {
            hashlib.sha256(json.dumps(r["messages"][:-1], ensure_ascii=False).encode()).hexdigest()
            for r in rows
        }
        combos = {tuple(r["condition"]) for r in rows}
        assert len(prompts) == len(rows)
        prompt_sets[split], condition_sets[split] = prompts, combos
        manifest["splits"][split] = {
            "rows": len(rows),
            "conditions": len(combos),
            "direct": sum(r["mode"] == "direct" for r in rows),
            "repair": sum(r["mode"] == "repair" for r in rows),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    for left, right in (("train", "valid"), ("train", "test"), ("valid", "test")):
        assert not prompt_sets[left] & prompt_sets[right]
        assert not condition_sets[left] & condition_sets[right]
    manifest["builder_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest["prompt_module_sha256"] = hashlib.sha256(
        (ROOT.parent / "energy_agent/sql_sft.py").read_bytes()
    ).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False, indent=2))
