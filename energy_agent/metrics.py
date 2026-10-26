"""Independent Python metric checks for a finite set of supported analysis contracts."""

import math
import statistics
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import sqlglot

from .data import readonly
from .errors import SemanticError
from .models import AnalysisTask, Scope
from .tasks import compile_task


def parse_utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def accepts_day(stamp: datetime, day_filter: str):
    return day_filter == "all" or ((stamp.weekday() < 5) == (day_filter == "weekday"))


def read_window(path: Path, scope: Scope):
    with closing(readonly(path)) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT timestamp_utc, load_mw, forecast_mw FROM load_hourly "
                "WHERE region=? AND timestamp_utc>=? AND timestamp_utc<? ORDER BY timestamp_utc",
                (scope.region, scope.start, scope.end),
            )
        ]


def bucket(stamp: datetime, granularity: str):
    if granularity == "hour":
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    if granularity == "day":
        return stamp.date().isoformat()
    if granularity == "week":
        return (stamp - timedelta(days=stamp.weekday())).date().isoformat()
    if granularity == "day_type":
        return "weekday" if stamp.weekday() < 5 else "weekend"
    return "total"


def quality(raw: list[dict], scope: Scope, task: AnalysisTask):
    selected = [row for row in raw if accepts_day(parse_utc(row["timestamp_utc"]), task.day_filter)]
    stamp, end = parse_utc(scope.start), parse_utc(scope.end)
    expected = 0
    while stamp < end:
        expected += int(accepts_day(stamp, task.day_filter))
        stamp += timedelta(hours=1)
    forecasts = [r for r in selected if r["forecast_mw"] is not None]
    return {
        "observed_hours": len(selected),
        "expected_hours": expected,
        "missing_hours": expected - len(selected),
        "forecast_hours": len(forecasts),
        "mape_hours": sum(r["load_mw"] > 0 for r in forecasts),
        "coverage_pct": round(100 * len(selected) / expected, 2) if expected else None,
        "calendar": "UTC; weekdays=Monday-Friday, no holiday calendar",
    }


def reference_rows(raw: list[dict], task: AnalysisTask):
    """No generated SQL is reused here. Units and NULL denominators are defined in Python."""
    rows = [row for row in raw if accepts_day(parse_utc(row["timestamp_utc"]), task.day_filter)]
    if task.kind == "count":
        return [{"n": len(rows)}]
    if task.kind == "peak":
        return sorted(rows, key=lambda r: (-r["load_mw"], r["timestamp_utc"]))[: task.top_k]
    grouped = defaultdict(list)
    if task.granularity == "total":
        grouped["total"] = rows
    else:
        for row in rows:
            grouped[bucket(parse_utc(row["timestamp_utc"]), task.granularity)].append(row)
    key = {"hour": "timestamp_utc", "day": "day", "week": "week", "day_type": "day_type"}.get(
        task.granularity
    )
    result = []
    for name, group in sorted(grouped.items()):
        actual = [r["load_mw"] for r in group]
        predicted = [r for r in group if r["forecast_mw"] is not None]
        item = {key: name} if key else {}
        item["observed_hours"] = len(group)
        if task.kind == "energy":
            item["energy_mwh"] = round(sum(actual), 2) if actual else None
        else:
            item["forecast_hours"] = len(predicted)
            if task.kind == "forecast":
                errors = [abs(r["forecast_mw"] - r["load_mw"]) for r in predicted]
                percentages = [
                    100 * abs(r["forecast_mw"] - r["load_mw"]) / r["load_mw"]
                    for r in predicted
                    if r["load_mw"] > 0
                ]
                item.update(
                    mae_mw=round(statistics.mean(errors), 3) if errors else None,
                    mse_mw2=round(statistics.mean(e * e for e in errors), 3) if errors else None,
                    mape_pct=round(statistics.mean(percentages), 3) if percentages else None,
                    mape_hours=len(percentages),
                )
            else:
                item["load_mw"] = round(statistics.mean(actual), 2) if actual else None
                item["forecast_mw"] = (
                    round(statistics.mean(r["forecast_mw"] for r in predicted), 2) if predicted else None
                )
        result.append(item)
    return result


def verify_result(raw: list[dict], task: AnalysisTask, result: dict, max_rows=500):
    if task.kind in ("custom", "anomaly"):
        return {"status": "not_covered", "checks": [], "limitation": "不声称验证开放式问题的业务语义。"}
    expected = reference_rows(raw, task)
    required = sqlglot.parse_one(compile_task(task).sql, read="sqlite").named_selects
    missing = set(required) - set(result["columns"])
    if missing:
        raise SemanticError("缺少任务要求的列：" + ", ".join(sorted(missing)))
    checks = ["任务字段和时间粒度", "数值与单位定义", "排序与 Top-K", "NULL 与指标分母"]
    rows = result["rows"]
    if len(rows) != min(len(expected), max_rows) or (result["truncated"] and len(expected) <= max_rows):
        raise SemanticError("结果行数与任务不符；检查时间分组、过滤、Top-K、LIMIT 和缺失值。")
    if len(expected) > max_rows:
        # A model may explicitly use LIMIT 500; the independent full result establishes truncation.
        result["truncated"] = True
    for wanted, actual in zip(expected[:max_rows], rows, strict=True):
        if "rmse_mw" in result["columns"] and "mse_mw2" in wanted:
            value = wanted["mse_mw2"]
            wanted["rmse_mw"] = round(math.sqrt(value), 3) if value is not None else None
        for column, value in wanted.items():
            if column not in result["columns"]:
                raise SemanticError(f"缺少任务要求的结果列 {column}，请使用合同中的别名。")
            got = actual.get(column)
            if isinstance(value, (int, float)):
                matches = (
                    isinstance(got, (int, float))
                    and math.isfinite(got)
                    and math.isclose(value, got, abs_tol=0.011, rel_tol=1e-9)
                )
            else:
                matches = got == value
            if not matches:
                raise SemanticError(f"{column} 不符合任务定义；检查聚合、单位、NULL 分母、排序及日类型。")
    return {
        "status": "verified",
        "checks": checks,
        "required_columns": required,
        "limitation": "只核验可解析的有限任务合同；无法证明任意自然语言解释正确。",
    }
