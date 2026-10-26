"""Real-source contract checks. This report is NOT an LLM performance benchmark."""

import csv
import hashlib
import json
import math
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .agent import ask
from .config import Settings
from .data import DataError, dataset_context, readonly
from .models import AskRequest
from .planner import DemoPlanner

CASES = Path(__file__).parent / "fixtures" / "real-data-cases.json"


def source_oracle(directory, source_entries, year):
    """Separate CSV decoder: no production EIA/metric/task compiler functions are used."""
    rows = {}
    for source in source_entries:
        path = directory / source["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != source["sha256"]:
            raise DataError("原始文件 SHA-256 与导入快照不同。")
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for item in csv.DictReader(stream):
                if item["Balancing Authority"] != "PJM" or not item["Demand (MW)"].strip():
                    continue
                stamp = datetime.strptime(item["UTC Time at End of Hour"], "%m/%d/%Y %I:%M:%S %p")
                start = stamp - timedelta(seconds=3600)
                if start.year != year:
                    continue
                key = start.strftime("%Y-%m-%dT%H:%M:%SZ")
                actual = float(item["Demand (MW)"].replace(",", ""))
                forecast = item["Demand Forecast (MW)"].strip()
                pair = (actual, float(forecast.replace(",", "")) if forecast else None)
                if key in rows and rows[key] != pair:
                    raise DataError("参考源存在冲突记录。")
                rows[key] = pair
    return [
        {"timestamp_utc": stamp, "load_mw": values[0], "forecast_mw": values[1]}
        for stamp, values in sorted(rows.items())
    ]


def expected_rows(raw, case):
    start = case["start"] + "T00:00:00Z"
    end = (datetime.fromisoformat(case["end"]) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    rows = [r for r in raw if start <= r["timestamp_utc"] < end]
    day_filter = case.get("day_filter", "all")
    if day_filter != "all":
        rows = [
            r
            for r in rows
            if (datetime.fromisoformat(r["timestamp_utc"]).weekday() < 5) == (day_filter == "weekday")
        ]
    kind, granularity = case["kind"], case.get("granularity", "day")
    if kind == "count":
        return [{"n": len(rows)}]
    if kind == "peak":
        return sorted(rows, key=lambda r: (-r["load_mw"], r["timestamp_utc"]))[: case.get("top_k", 5)]
    groups = defaultdict(list)
    for row in rows:
        stamp = datetime.fromisoformat(row["timestamp_utc"])
        key = (
            row["timestamp_utc"]
            if granularity == "hour"
            else (stamp - timedelta(days=stamp.weekday())).date().isoformat()
            if granularity == "week"
            else "weekday"
            if granularity == "day_type" and stamp.weekday() < 5
            else "weekend"
            if granularity == "day_type"
            else "total"
            if granularity == "total"
            else stamp.date().isoformat()
        )
        groups[key].append(row)
    if granularity == "total" and not groups:
        groups["total"] = []
    keys = {"hour": "timestamp_utc", "day": "day", "week": "week", "day_type": "day_type"}
    result = []
    for key, items in sorted(groups.items()):
        item = {keys[granularity]: key} if granularity in keys else {}
        n = len(items)
        forecasts = [r for r in items if r["forecast_mw"] is not None]
        item["observed_hours"] = n
        if kind == "energy":
            item["energy_mwh"] = round(math.fsum(r["load_mw"] for r in items), 2) if n else None
        else:
            item["forecast_hours"] = len(forecasts)
            if kind == "trend":
                item["load_mw"] = round(math.fsum(r["load_mw"] for r in items) / n, 2) if n else None
                item["forecast_mw"] = (
                    round(math.fsum(r["forecast_mw"] for r in forecasts) / len(forecasts), 2)
                    if forecasts
                    else None
                )
            else:
                errors = [abs(r["load_mw"] - r["forecast_mw"]) for r in forecasts]
                percentages = [
                    100 * abs(r["load_mw"] - r["forecast_mw"]) / r["load_mw"]
                    for r in forecasts
                    if r["load_mw"] > 0
                ]
                item.update(
                    mae_mw=round(math.fsum(errors) / len(errors), 3) if errors else None,
                    mse_mw2=round(math.fsum(v * v for v in errors) / len(errors), 3) if errors else None,
                    mape_pct=round(math.fsum(percentages) / len(percentages), 3) if percentages else None,
                    mape_hours=len(percentages),
                )
        result.append(item)
    return result


def matches(expected, actual):
    if len(expected) != len(actual):
        return False
    for wanted, got in zip(expected, actual, strict=True):
        for key, value in wanted.items():
            observed = got.get(key)
            if isinstance(value, (float, int)):
                if not isinstance(observed, (float, int)) or not math.isclose(value, observed, abs_tol=0.011):
                    return False
            elif value != observed:
                return False
    return True


def validate_real(settings: Settings, sources: Path, output: Path, *, figures: Path | None = None):
    context = dataset_context(settings.db_path)
    metadata = context["metadata"]
    if metadata.get("source_kind") != "eia930":
        raise DataError("此校验要求 import-eia 生成的数据库。")
    raw = source_oracle(sources, metadata["sources"], metadata["audit"]["year"])
    with closing(readonly(settings.db_path)) as conn:
        stored = [
            dict(r)
            for r in conn.execute(
                "SELECT timestamp_utc, load_mw, forecast_mw FROM load_hourly WHERE region='PJM' "
                "ORDER BY timestamp_utc"
            )
        ]
    row_integrity = stored == raw
    fixture = json.loads(CASES.read_text())
    records = []
    for case in fixture["cases"]:
        response = ask(
            settings,
            AskRequest(
                question=case["question"], region="PJM", start_date=case["start"], end_date=case["end"]
            ),
            planner=DemoPlanner(),
        )
        gold = expected_rows(raw, case)
        records.append(
            {
                "id": case["id"],
                "split": case["split"],
                "question": case["question"],
                "start": case["start"],
                "end": case["end"],
                "correct": response["status"] != "error"
                and matches(gold[: settings.max_rows], response["result"]["rows"]),
                "expected_rows": len(gold),
                "returned_rows": len(response["result"]["rows"]),
                "outcome": response["outcome"],
                "quality": response["quality"],
                "latency_ms": response["latency_ms"],
                "expected": gold[: settings.max_rows],
                "actual": response["result"]["rows"],
            }
        )
    groups = defaultdict(list)
    for row in raw:
        groups[row["timestamp_utc"][:10]].append(row)
    daily = [
        {
            "day": key,
            "observed_hours": len(items),
            "mean_load_mw": round(math.fsum(r["load_mw"] for r in items) / len(items), 2),
            "energy_mwh": math.fsum(r["load_mw"] for r in items),
        }
        for key, items in sorted(groups.items())
    ]
    result = {
        "benchmark": "eia-pjm-contract-pilot-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "planner": "demo",
        "model_calls": 0,
        "row_integrity": row_integrity,
        "source_rows_checked": len(raw),
        "source": metadata,
        "case_sha256": hashlib.sha256(CASES.read_bytes()).hexdigest(),
        "summary": {
            split: {
                "cases": sum(r["split"] == split for r in records),
                "correct": sum(r["split"] == split and r["correct"] for r in records),
            }
            for split in ("dev", "test")
        },
        "limitations": "Same-author contract pilot on real reported data; separate source CSV oracle. "
        "Dev/test use different time windows but share metric families. No external question review, "
        "no LLM benchmark, no model training, no causal anomaly or forecasting-backtest claim.",
        "records": records,
        "daily": daily,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if figures:
        export_real_figures(result, figures)
        (figures / "real-chart-values.json").write_text(
            json.dumps(
                {
                    "source_report": output.name,
                    "source_report_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
                    "source_csv_sha256": hashlib.sha256(
                        (figures / "real-daily-load.csv").read_bytes()
                    ).hexdigest(),
                    "source_snapshots": metadata["sources"],
                    "audit": metadata["audit"],
                    "partial_days": [r for r in daily if r["observed_hours"] != 24],
                    "limitations": result["limitations"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    return result


def export_real_figures(report, directory):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    directory.mkdir(parents=True, exist_ok=True)
    daily = report["daily"]
    with (directory / "real-daily-load.csv").open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(daily[0]))
        writer.writeheader()
        writer.writerows(daily)
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "svg.fonttype": "none",
        }
    )
    fig, axes = plt.subplots(2, 1, figsize=(11, 6.6), gridspec_kw={"height_ratios": [2, 1]})
    dates = [datetime.fromisoformat(r["day"]) for r in daily]
    axes[0].plot(
        dates,
        [r["mean_load_mw"] / 1000 if r["observed_hours"] == 24 else math.nan for r in daily],
        color="#37735b",
        linewidth=1.4,
        label="24/24 observed hours",
    )
    partial = [r for r in daily if r["observed_hours"] != 24]
    axes[0].scatter(
        [datetime.fromisoformat(r["day"]) for r in partial],
        [r["mean_load_mw"] / 1000 for r in partial],
        color="#ac7549",
        s=25,
        label="Partial-day mean",
        zorder=3,
    )
    axes[0].legend(frameon=False, loc="upper right", fontsize=8)
    axes[0].set(ylabel="Observed daily mean (GW)", title="PJM hourly demand | actual EIA-930 source data")
    axes[0].grid(axis="y", alpha=0.16)
    axes[1].bar(dates, [24 - r["observed_hours"] for r in daily], color="#ac7549", width=2)
    axes[1].set(ylabel="Missing hours / UTC day", ylim=(0, 24), yticks=[0, 6, 12, 18, 24])
    axes[1].grid(axis="y", alpha=0.16)
    audit = report["source"]["audit"]
    fig.text(
        0.08,
        0.045,
        f"{audit['rows']:,} observed / {audit['expected_hours']:,} expected hours; "
        f"{audit['missing_hours']} missing, not imputed. Daily means use observed hours only.",
        fontsize=10,
    )
    fig.text(
        0.08,
        0.014,
        "Source: U.S. Energy Information Administration | snapshots and hashes in "
        "reports/eia-provenance.json. This chart measures data coverage, not LLM accuracy.",
        fontsize=8,
        color="#596465",
    )
    fig.tight_layout(rect=(0.03, 0.09, 0.99, 0.99), h_pad=2)
    for extension in ("png", "svg"):
        fig.savefig(directory / f"real-pjm-overview.{extension}", dpi=160)
    plt.close(fig)
