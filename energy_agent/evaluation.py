"""Versioned synthetic benchmark with typed outcomes and independent Python gold values."""

import json
import math
import shutil
import sqlite3
import statistics
import tempfile
import time
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

from .agent import ask, empty_result
from .config import Settings
from .data import create_demo, readonly
from .errors import PlanningError, failure_outcome
from .models import AskRequest
from .planner import DemoPlanner

CASES = [
    {"id": "daily-zh", "question": "最近7天的负荷趋势", "region": "PJM_RTO", "kind": "trend"},
    {"id": "daily-en", "question": "daily load trend for last 7 days", "region": "DOM", "kind": "trend"},
    {
        "id": "explicit-range",
        "question": "2025-01-03 到 2025-01-05 的负荷趋势",
        "region": "AECO",
        "kind": "trend",
        "start": "2025-01-03",
        "end": "2025-01-05",
    },
    {
        "id": "hourly",
        "question": "最近2天的逐小时负荷",
        "region": "DOM",
        "kind": "trend",
        "grain": "hour",
        "days": 2,
    },
    {"id": "peak", "question": "最近7天负荷最高的5个小时", "region": "PJM_RTO", "kind": "peak"},
    {"id": "forecast", "question": "最近7天的每日预测误差 MAE MAPE", "region": "PJM_RTO", "kind": "forecast"},
    {"id": "energy", "question": "最近7天每日观测电量 MWh", "region": "AECO", "kind": "energy"},
    {"id": "anomaly", "question": "最近7天有哪些负荷异常", "region": "DOM", "kind": "anomaly"},
    {
        "id": "out-of-range",
        "question": "2030-01-01 的负荷趋势",
        "region": "PJM_RTO",
        "kind": "reject",
        "expected_error": "invalid_scope",
        "expected_outcome": "rejected",
    },
    {
        "id": "write-refusal",
        "question": "DELETE FROM load_hourly",
        "region": "PJM_RTO",
        "kind": "reject",
        "expected_error": "policy_refusal",
        "expected_outcome": "refused",
    },
    {
        "id": "missing-evidence",
        "question": "最近7天的电价走势 price",
        "region": "DOM",
        "kind": "reject",
        "expected_error": "data_unavailable",
        "expected_outcome": "refused",
    },
    {
        "id": "top10-zh",
        "question": "最近7天负荷最高的10个小时",
        "region": "PJM_RTO",
        "kind": "peak",
        "top_k": 10,
    },
    {
        "id": "top10-en",
        "question": "top 10 peak load hours in last 7 days",
        "region": "DOM",
        "kind": "peak",
        "top_k": 10,
    },
    {
        "id": "weekly",
        "question": "2025-01-01 到 2025-01-28 按周的负荷趋势",
        "region": "AECO",
        "kind": "trend",
        "start": "2025-01-01",
        "end": "2025-01-28",
        "grain": "week",
    },
    {
        "id": "weekday-vs-weekend",
        "question": "最近7天工作日与周末负荷比较",
        "region": "DOM",
        "kind": "trend",
        "grain": "day_type",
    },
    {
        "id": "weekend-filter",
        "question": "最近7天周末负荷趋势",
        "region": "DOM",
        "kind": "trend",
        "filter": "weekend",
    },
    {
        "id": "energy-total",
        "question": "最近7天总用电量 MWh",
        "region": "AECO",
        "kind": "energy",
        "grain": "total",
    },
    {
        "id": "forecast-total",
        "question": "最近7天整体预测误差",
        "region": "DOM",
        "kind": "forecast",
        "grain": "total",
    },
    {"id": "row-count", "question": "最近7天负荷记录数", "region": "DOM", "kind": "count"},
    {
        "id": "gaps",
        "question": "最近7天每日观测电量",
        "region": "PJM_RTO",
        "kind": "energy",
        "fixture": "gaps",
    },
    {
        "id": "missing-forecast",
        "question": "最近7天预测误差",
        "region": "PJM_RTO",
        "kind": "forecast",
        "fixture": "null_forecast",
        "expected_outcome": "insufficient_data",
    },
    {
        "id": "zero-actual-mape",
        "question": "最近7天整体预测误差",
        "region": "PJM_RTO",
        "kind": "forecast",
        "fixture": "zero_actual",
        "grain": "total",
    },
    {
        "id": "cold-start-anomaly",
        "question": "2025-01-01 的负荷异常",
        "region": "DOM",
        "kind": "anomaly",
        "start": "2025-01-01",
        "end": "2025-01-01",
        "expected_outcome": "insufficient_data",
    },
    {
        "id": "invalid-topk",
        "question": "最近7天负荷 top 101",
        "region": "DOM",
        "kind": "reject",
        "expected_error": "unsupported_question",
        "expected_outcome": "refused",
    },
    {"id": "top100", "question": "最近7天负荷 top 100", "region": "DOM", "kind": "peak", "top_k": 100},
    {
        "id": "weekday-forecast",
        "question": "最近7天工作日与周末预测误差",
        "region": "AECO",
        "kind": "forecast",
        "grain": "day_type",
    },
]


def gold_rows(path: Path, case: dict):
    """Deliberately does not call task parser, SQL compiler, runtime verifier or anomaly tool."""
    with closing(readonly(path)) as conn:
        all_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM load_hourly WHERE region=? ORDER BY timestamp_utc", (case["region"],)
            )
        ]
    end = case.get("end", all_rows[-1]["timestamp_utc"][:10])
    start = case.get(
        "start", (datetime.fromisoformat(end) - timedelta(days=case.get("days", 7) - 1)).date().isoformat()
    )
    rows = [r for r in all_rows if start <= r["timestamp_utc"][:10] <= end]
    selected = case.get("filter", "all")
    if selected != "all":
        rows = [
            r
            for r in rows
            if (
                (datetime.fromisoformat(r["timestamp_utc"].replace("Z", "+00:00")).weekday() < 5)
                == (selected == "weekday")
            )
        ]
    kind = case["kind"]
    if kind == "count":
        return [{"n": len(rows)}]
    if kind == "peak":
        return [
            {k: r[k] for k in ("timestamp_utc", "load_mw", "forecast_mw")}
            for r in sorted(rows, key=lambda r: (-r["load_mw"], r["timestamp_utc"]))[: case.get("top_k", 5)]
        ]
    if kind == "anomaly":
        output = []
        for row in rows:
            stamp = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
            history = []
            for previous in all_rows:
                past = datetime.fromisoformat(previous["timestamp_utc"].replace("Z", "+00:00"))
                if (
                    stamp - timedelta(days=28) <= past < stamp
                    and past.hour == stamp.hour
                    and (past.weekday() < 5) == (stamp.weekday() < 5)
                ):
                    history.append(previous["load_mw"])
            if len(history) < 4:
                continue
            center = statistics.median(history)
            mad = statistics.median(abs(v - center) for v in history)
            score = (row["load_mw"] - center) / max(1.4826 * mad, abs(center) * 0.01, 1.0)
            if abs(score) >= 3.5:
                output.append(
                    {
                        "timestamp_utc": row["timestamp_utc"],
                        "load_mw": row["load_mw"],
                        "baseline_mw": round(center, 3),
                        "mad_mw": round(mad, 3),
                        "robust_z": round(score, 3),
                        "history_points": len(history),
                    }
                )
        return sorted(output, key=lambda r: (-abs(r["robust_z"]), r["timestamp_utc"]))[
            : case.get("top_k", 20)
        ]
    grain = case.get("grain", "day")
    grouped = defaultdict(list)
    if grain == "total":
        grouped["total"] = rows
    for row in rows if grain != "total" else []:
        stamp = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
        label = (
            row["timestamp_utc"]
            if grain == "hour"
            else (stamp - timedelta(days=stamp.weekday())).date().isoformat()
            if grain == "week"
            else ("weekday" if stamp.weekday() < 5 else "weekend")
            if grain == "day_type"
            else stamp.date().isoformat()
        )
        grouped[label].append(row)
    output = []
    key = {"hour": "timestamp_utc", "day": "day", "week": "week", "day_type": "day_type"}.get(grain)
    for label, group in sorted(grouped.items()):
        actual = [r["load_mw"] for r in group]
        forecast = [r for r in group if r["forecast_mw"] is not None]
        item = {key: label} if key else {}
        item["observed_hours"] = len(group)
        if kind == "energy":
            item["energy_mwh"] = round(sum(actual), 2) if actual else None
        elif kind == "trend":
            item.update(
                load_mw=round(statistics.mean(actual), 2) if actual else None,
                forecast_mw=round(statistics.mean(r["forecast_mw"] for r in forecast), 2)
                if forecast
                else None,
                forecast_hours=len(forecast),
            )
        elif kind == "forecast":
            errors = [abs(r["forecast_mw"] - r["load_mw"]) for r in forecast]
            percentages = [
                100 * abs(r["forecast_mw"] - r["load_mw"]) / r["load_mw"]
                for r in forecast
                if r["load_mw"] > 0
            ]
            item.update(
                forecast_hours=len(forecast),
                mape_hours=len(percentages),
                mae_mw=round(statistics.mean(errors), 3) if errors else None,
                mse_mw2=round(statistics.mean(e * e for e in errors), 3) if errors else None,
                mape_pct=round(statistics.mean(percentages), 3) if percentages else None,
            )
        output.append(item)
    return output


def compare_rows(expected, actual):
    if len(expected) != len(actual):
        return False
    for wanted, got in zip(expected, actual, strict=True):
        for key, value in wanted.items():
            if key not in got:
                return False
            if isinstance(value, (int, float)):
                if (
                    not isinstance(got[key], (int, float))
                    or not math.isfinite(got[key])
                    or not math.isclose(got[key], value, abs_tol=0.011, rel_tol=1e-9)
                ):
                    return False
            elif got[key] != value:
                return False
    return True


def score_case(case, response, expected):
    if case["kind"] == "reject":
        return (
            response.get("outcome") == case["expected_outcome"]
            and response.get("error_code") == case["expected_error"]
        )
    expected_outcome = case.get("expected_outcome", "success" if expected else "empty")
    return (
        response.get("outcome") == expected_outcome
        and not response.get("error_code")
        and compare_rows(expected, response["result"]["rows"])
    )


def _fixtures(directory: Path):
    base = directory / "base.sqlite"
    create_demo(base)
    paths = {"base": base}
    statements = {
        "gaps": "DELETE FROM load_hourly WHERE region='PJM_RTO' AND timestamp_utc IN "
        "('2025-01-23T03:00:00Z', '2025-01-23T04:00:00Z')",
        "null_forecast": "UPDATE load_hourly SET forecast_mw=NULL WHERE region='PJM_RTO'",
        "zero_actual": "UPDATE load_hourly SET load_mw=0 WHERE region='PJM_RTO' AND timestamp_utc IN "
        "('2025-01-24T03:00:00Z', '2025-01-24T04:00:00Z')",
    }
    for name, sql in statements.items():
        path = directory / f"{name}.sqlite"
        shutil.copyfile(base, path)
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(sql)
        paths[name] = path
    return paths


class InjectedSemanticPlanner(DemoPlanner):
    """Explicit engineering diagnostic, never presented as actual LLM behavior."""

    def __init__(self, fault):
        super().__init__()
        self.fault = fault

    def plan(self, question, context, scope, previous=None):
        plan = super().plan(question, context, scope, previous)
        if previous and previous.get("error_code") == "semantic_error":
            return plan
        replacements = {
            "power_sum": ("AVG(load_mw)", "SUM(load_mw)"),
            "energy_average": ("SUM(load_mw)", "AVG(load_mw)"),
            "wrong_topk": ("LIMIT 10", "LIMIT 5"),
            "null_as_zero": ("ABS(forecast_mw - load_mw)", "ABS(COALESCE(forecast_mw, 0) - load_mw)"),
        }
        old, new = replacements[self.fault]
        return plan.model_copy(update={"sql": plan.sql.replace(old, new)})


def semantic_diagnostics(fixtures):
    definitions = [
        ("power_sum", {"question": "最近7天负荷趋势", "kind": "trend"}),
        ("energy_average", {"question": "最近7天每日电量", "kind": "energy"}),
        ("wrong_topk", {"question": "最近7天负荷 top 10", "kind": "peak", "top_k": 10}),
        (
            "null_as_zero",
            {
                "question": "最近7天预测误差",
                "kind": "forecast",
                "fixture": "null_forecast",
                "expected_outcome": "insufficient_data",
            },
        ),
    ]
    records = []
    for mode in ("baseline", "agent", "domain"):
        for fault, spec in definitions:
            case = {"region": "PJM_RTO", **spec}
            path = fixtures[case.get("fixture", "base")]
            response = ask(
                Settings(path),
                AskRequest(question=case["question"], region=case["region"]),
                planner=InjectedSemanticPlanner(fault),
                mode=mode,
            )
            records.append(
                {
                    "fault": fault,
                    "mode": mode,
                    "correct": score_case(case, response, gold_rows(path, case)),
                    "outcome": response["outcome"],
                    "retries": response["retries"],
                    "trace": response["trace"],
                }
            )
    return {
        "label": "Injected executable-but-wrong SQL; engineering diagnostic, NOT an LLM benchmark.",
        "summary": {
            mode: {"correct": sum(r["correct"] for r in records if r["mode"] == mode), "cases": 4}
            for mode in ("baseline", "agent", "domain")
        },
        "records": records,
    }


def evaluate(*, planner="demo", model="gpt-4.1-mini", output: Path | None = None):
    if planner not in ("demo", "openai"):
        raise ValueError("Legacy batch benchmark supports only demo or explicit openai, not the tool loop.")
    records = []
    with tempfile.TemporaryDirectory() as directory:
        fixtures = _fixtures(Path(directory))
        for mode in ("baseline", "agent", "domain"):
            for case in CASES:
                path = fixtures[case.get("fixture", "base")]
                started = time.perf_counter()
                try:
                    response = ask(
                        Settings(path, planner=planner, model=model),
                        AskRequest(question=case["question"], region=case["region"]),
                        mode=mode,
                    )
                except PlanningError as exc:
                    response = {
                        "status": "error",
                        "outcome": failure_outcome(exc.code),
                        "error_code": exc.code,
                        "result": empty_result(),
                        "latency_ms": round((time.perf_counter() - started) * 1000, 2),
                        "usage": {
                            "calls": 0,
                            "input_tokens": 0,
                            "output_tokens": 0,
                            "estimated_cost_usd": None,
                        },
                        "answer": str(exc),
                        "sql": "",
                        "trace": [],
                        "retries": 0,
                    }
                expected = [] if case["kind"] == "reject" else gold_rows(path, case)
                records.append(
                    {
                        "case": case["id"],
                        "mode": mode,
                        "correct": score_case(case, response, expected),
                        "answerable": case["kind"] != "reject",
                        "outcome": response["outcome"],
                        "error_code": response["error_code"],
                        "status": response["status"],
                        "latency_ms": response["latency_ms"],
                        "retries": response["retries"],
                        "usage": response["usage"],
                        "sql": response["sql"],
                        "answer": response["answer"],
                        "trace": response["trace"],
                    }
                )
        diagnostics = semantic_diagnostics(fixtures)
    summary = {}
    for mode in ("baseline", "agent", "domain"):
        items = [r for r in records if r["mode"] == mode]
        answerable = [r for r in items if r["answerable"]]
        rejections = [r for r in items if not r["answerable"]]
        latencies = sorted(r["latency_ms"] for r in items)
        summary[mode] = {
            "cases": len(items),
            "correct": sum(r["correct"] for r in items),
            "end_to_end_accuracy": sum(r["correct"] for r in items) / len(items),
            "answerable_accuracy": sum(r["correct"] for r in answerable) / len(answerable),
            "typed_rejection_accuracy": sum(r["correct"] for r in rejections) / len(rejections),
            "system_failures": sum(r["outcome"] == "failed" for r in items),
            "median_latency_ms": round(statistics.median(latencies), 2),
            "p95_latency_ms": latencies[math.ceil(len(latencies) * 0.95) - 1],
            "model_calls": sum(r["usage"]["calls"] for r in items),
            "input_tokens": sum(r["usage"]["input_tokens"] for r in items),
            "output_tokens": sum(r["usage"]["output_tokens"] for r in items),
            "estimated_cost_usd": (
                sum(r["usage"]["estimated_cost_usd"] for r in items)
                if all(r["usage"]["estimated_cost_usd"] is not None for r in items)
                else None
            ),
        }
    report = {
        "benchmark": "energy-contract-v2",
        "planner": planner,
        "model": model if planner == "openai" else None,
        "limitations": "26 curated synthetic cases, not held-out real-PJM evidence. "
        "All pipelines share scope/read-only "
        "guards, task contracts and the causal anomaly tool. Baseline is one-shot; agent adds syntax "
        "preflight/repair; domain adds independent metric verification. Extra columns are ignored; "
        "required columns, stable row order and typed outcomes must match. Cold starts are explicit. "
        "Diagnostic faults are injected, not real LLM performance gains.",
        "summary": summary,
        "records": records,
        "semantic_diagnostics": diagnostics,
    }
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
