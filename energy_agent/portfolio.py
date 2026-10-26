"""Export inspectable project evidence as standalone PNG/SVG figures and source tables."""

import csv
import hashlib
import json
import math
import os
import statistics
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .anomaly import detect_anomalies
from .data import DataError, create_demo, dataset_context, readonly
from .models import AnalysisTask, Scope

MODES = ("baseline", "agent", "domain")
NAMES = {"baseline": "One-shot", "agent": "Basic agent", "domain": "Domain checks"}
COLORS = {"baseline": "#b8c4bd", "agent": "#7c9587", "domain": "#265c46"}
FAULTS = {
    "power_sum": "Power: SUM instead of AVG",
    "energy_average": "Energy: AVG instead of SUM",
    "wrong_topk": "Top 10 query returns 5 rows",
    "null_as_zero": "Missing forecast filled with 0",
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def review_report(report):
    """Recompute plotted figures from records; reject contradictory or non-finite summaries."""
    summary, diagnostics = {}, {}
    for mode in MODES:
        rows = [r for r in report["records"] if r["mode"] == mode]
        if not rows:
            raise DataError(f"评测缺少 {mode} 的逐题记录。")
        if len({r["case"] for r in rows}) != len(rows):
            raise DataError("评测题目 ID 重复，无法建立一致的比较分母。")
        times = [r["latency_ms"] for r in rows]
        if any(not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0 for t in times):
            raise DataError("延迟记录含无效数值。")
        summary[mode] = {
            "cases": len(rows),
            "correct": sum(r["correct"] for r in rows),
            "p50_ms": round(statistics.median(times), 2),
            "p95_ms": sorted(times)[math.ceil(len(times) * 0.95) - 1],
            "model_calls": sum(r["usage"]["calls"] for r in rows),
        }
        original = report["summary"][mode]
        if summary[mode]["cases"] != original["cases"] or summary[mode]["correct"] != original["correct"]:
            raise DataError("汇总分母或正确数与逐题记录不一致。")
        if not math.isclose(summary[mode]["p50_ms"], original["median_latency_ms"], abs_tol=0.011):
            raise DataError("P50 汇总与逐题记录不一致。")
        records = [r for r in report["semantic_diagnostics"]["records"] if r["mode"] == mode]
        if len(records) != len(FAULTS) or {r["fault"] for r in records} != set(FAULTS):
            raise DataError("故障注入记录不完整或包含重复项。")
        diagnostics[mode] = {r["fault"]: bool(r["correct"]) for r in records}
    populations = [{r["case"] for r in report["records"] if r["mode"] == mode} for mode in MODES]
    if not all(population == populations[0] for population in populations):
        raise DataError("三个流程使用的题集不同，不能画成同组对照。")
    if report["planner"] == "demo" and any(s["model_calls"] for s in summary.values()):
        raise DataError("演示模式标签与模型调用记录冲突。")
    return summary, diagnostics


def read_tests(path):
    if path is None or not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
        cases = root.findall(".//testcase")
        failed = sum(case.find("failure") is not None or case.find("error") is not None for case in cases)
        skipped = sum(case.find("skipped") is not None for case in cases)
        return {
            "total": len(cases),
            "passed": len(cases) - failed - skipped,
            "failed": failed,
            "skipped": skipped,
            "sha256": digest(path),
        }
    except (ET.ParseError, OSError) as exc:
        raise DataError("测试证据不是可解析的 JUnit XML。") from exc


def demo_evidence():
    """An isolated seed-42 case study, never a substitute for real PJM observations."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "demo.sqlite"
        create_demo(path)
        context = dataset_context(path)
        with closing(readonly(path)) as conn:
            all_rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT timestamp_utc, load_mw, forecast_mw FROM load_hourly "
                    "WHERE region='PJM_RTO' ORDER BY timestamp_utc"
                )
            ]
        scope = Scope(start="2025-01-22T00:00:00Z", end="2025-01-29T00:00:00Z", region="PJM_RTO")
        anomaly = detect_anomalies(path, scope, AnalysisTask(kind="anomaly", top_k=100))
    window = [r for r in all_rows if scope.start <= r["timestamp_utc"] < scope.end]
    flagged = {r["timestamp_utc"] for r in anomaly["rows"]}
    # These labels follow the fixed generator's explicit 3-hour intervention, not detector output.
    injected = {f"2025-01-22T{hour:02}:00:00Z" for hour in (17, 18, 19)}
    daily = defaultdict(list)
    for row in window:
        row["detector_flag"] = row["timestamp_utc"] in flagged
        row["known_injection"] = row["timestamp_utc"] in injected
        daily[row["timestamp_utc"][:10]].append(row)
    errors = [
        {
            "day": day,
            "mae_mw": round(statistics.mean(abs(r["forecast_mw"] - r["load_mw"]) for r in rows), 3),
            "forecast_hours": sum(r["forecast_mw"] is not None for r in rows),
        }
        for day, rows in sorted(daily.items())
    ]
    return {
        "synthetic": True,
        "seed": 42,
        "region": "PJM_RTO",
        "scope": scope.model_dump(),
        "dataset_rows": sum(r["rows"] for r in context["regions"]),
        "regions": len(context["regions"]),
        "days_per_region": len({r["timestamp_utc"][:10] for r in all_rows}),
        "observed_hours": len(window),
        "expected_hours": 168,
        "flagged_hours": len(flagged),
        "known_injected_hours": len(injected),
        "known_injected_flagged": len(injected & flagged),
        "additional_flags": len(flagged - injected),
        "anomaly_evidence": anomaly["anomaly_evidence"],
        "daily_mae": errors,
        "hourly": window,
    }


def write_csv(path, rows):
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def plotting():
    os.environ.setdefault("MPLCONFIGDIR", str(Path.cwd() / ".mpl-cache"))
    os.environ.setdefault("XDG_CACHE_HOME", str(Path.cwd() / ".mpl-cache"))
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise DataError("可视化需安装可选依赖：uv sync --group viz") from exc
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "font.size": 10,
            "axes.titlesize": 12,
            "axes.labelsize": 10,
            "axes.edgecolor": "#d5ddd6",
            "axes.labelcolor": "#53675b",
            "text.color": "#233b2d",
            "xtick.color": "#65796b",
            "ytick.color": "#65796b",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "figure.facecolor": "#f7f9f5",
            "axes.facecolor": "white",
            "grid.color": "#e9eee7",
            "grid.linewidth": 0.7,
            "axes.axisbelow": True,
            "svg.fonttype": "path",
            "savefig.facecolor": "#f7f9f5",
        }
    )
    return plt


def save_figure(fig, output, name):
    for suffix in ("png", "svg"):
        fig.savefig(
            output / f"{name}.{suffix}",
            dpi=180,
            bbox_inches="tight",
            pad_inches=0.22,
            metadata={"Creator": "Gridnote evidence export"},
        )


def benchmark_figure(plt, output, evidence):
    import numpy as np
    from matplotlib.colors import ListedColormap

    summary, faults, tests, demo = (evidence[k] for k in ("summary", "diagnostics", "tests", "demo"))
    fig = plt.figure(figsize=(14, 9.8))
    fig.text(0.06, 0.965, "GRIDNOTE / PROJECT EVIDENCE", color="#63816b", size=10, weight="bold")
    fig.text(0.06, 0.923, "What the current evaluation demonstrates", size=23, weight="bold")
    mode_note = (
        "Rule-based demo; no live LLM calls"
        if evidence["planner"] == "demo"
        else f"Planner: {evidence['planner']}"
    )
    fig.text(
        0.06,
        0.89,
        f"{mode_note}  |  {evidence['benchmark']}  |  Fixed synthetic task set",
        size=10,
        color="#7a887c",
    )
    cards = [
        (f"{demo['dataset_rows']:,}", "synthetic hourly rows"),
        (str(summary["domain"]["cases"]), "curated questions / pipeline"),
        (str(len(FAULTS)), "scripted SQL fault types"),
        (f"{tests['passed']}/{tests['total']}" if tests else "not recorded", "automated tests passed"),
    ]
    for i, (value, label) in enumerate(cards):
        left = 0.06 + i * 0.235
        fig.text(left, 0.824, value, size=24, weight="bold", color="#265c46")
        fig.text(left, 0.796, label, size=9, color="#7a887c")

    grid = fig.add_gridspec(
        2,
        2,
        left=0.09,
        right=0.96,
        bottom=0.13,
        top=0.725,
        height_ratios=[1.05, 1],
        width_ratios=[1, 1.1],
        hspace=0.68,
        wspace=0.5,
    )
    ax = fig.add_subplot(grid[0, 0])
    for i, mode in enumerate(MODES):
        correct, count = summary[mode]["correct"], summary[mode]["cases"]
        ax.barh(i, correct, color=COLORS[mode], height=0.5)
        ax.text(correct + 0.5, i, f"{correct}/{count}", va="center", weight="bold", size=11)
    ax.set_yticks(range(3), [NAMES[m] for m in MODES])
    ax.invert_yaxis()
    ax.set_xlim(0, max(s["cases"] for s in summary.values()) * 1.24)
    ax.set_xticks([0, 5, 10, 15, 20, 26])
    ax.grid(axis="x")
    ax.set_title("A  Curated case checks", loc="left", pad=18, weight="bold")
    ax.set_xlabel("Correct outcomes, including typed rejection checks")
    ax.text(
        0,
        -0.26,
        "All three pass; this chart shows no accuracy gain.",
        transform=ax.transAxes,
        size=9,
        color="#7a887c",
    )

    ax = fig.add_subplot(grid[0, 1])
    matrix = np.array([[int(faults[mode][fault]) for mode in MODES] for fault in FAULTS])
    ax.imshow(matrix, cmap=ListedColormap(["#edf0e9", "#265c46"]), vmin=0, vmax=1, aspect="auto")
    ax.set_xticks(range(3), [NAMES[m] for m in MODES], size=9)
    ax.set_yticks(range(4), ["Power aggregation", "Energy aggregation", "Top-K", "Missing forecasts"], size=9)
    ax.tick_params(length=0, pad=9)
    for spine in ax.spines.values():
        spine.set_visible(False)
    for row in range(4):
        for col in range(3):
            ax.text(
                col,
                row,
                "recovered" if matrix[row, col] else "missed",
                ha="center",
                va="center",
                color="white" if matrix[row, col] else "#859186",
                size=9,
            )
    ax.set_title("B  Injected SQL faults (4 scripted scenarios)", loc="left", pad=18, weight="bold")
    ax.text(
        0,
        -0.26,
        "Scripted recovery after feedback; not an LLM benchmark.",
        transform=ax.transAxes,
        size=9,
        color="#7a887c",
    )

    ax = fig.add_subplot(grid[1, :])
    positions = np.arange(3)
    width = 0.27
    for offset, key, label, color in [
        (-width / 2, "p50_ms", "P50", "#265c46"),
        (width / 2, "p95_ms", "P95", "#a7b9ac"),
    ]:
        values = [summary[m][key] for m in MODES]
        bars = ax.bar(positions + offset, values, width, label=label, color=color)
        for bar, value in zip(bars, values, strict=True):
            ax.annotate(
                f"{value:.2f}",
                (bar.get_x() + bar.get_width() / 2, value),
                xytext=(0, 5),
                textcoords="offset points",
                ha="center",
                size=10,
            )
    ax.set_xticks(positions, [NAMES[m] for m in MODES])
    ax.set_ylabel("Milliseconds")
    ax.set_ylim(0, max(summary[m]["p95_ms"] for m in MODES) * 1.3)
    ax.grid(axis="y")
    ax.legend(frameon=False, ncol=2, loc="upper right")
    ax.set_title("C  Local observed runtime, one run", loc="left", pad=14, weight="bold")
    fig.text(
        0.09,
        0.065,
        f"Latency covers {summary['domain']['cases']} mixed tasks per pipeline in one local run; "
        "not a service SLA or model/network latency.",
        size=9,
        color="#7a887c",
    )
    fig.text(
        0.09,
        0.038,
        "Source: evaluation records + JUnit XML. "
        "Synthetic and scripted evidence cannot establish real-world LLM gains.",
        size=9,
        color="#7a887c",
    )
    save_figure(fig, output, "evaluation-overview")
    plt.close(fig)


def business_figure(plt, output, demo):
    import matplotlib.dates as mdates

    rows = demo["hourly"]
    times = [datetime.fromisoformat(r["timestamp_utc"].replace("Z", "+00:00")) for r in rows]
    fig = plt.figure(figsize=(14, 9.2))
    fig.text(0.07, 0.965, "GRIDNOTE / ENERGY CASE STUDY", size=10, color="#63816b", weight="bold")
    fig.text(0.07, 0.923, "Load, forecast error and a visible failure case", size=22, weight="bold")
    fig.text(
        0.07,
        0.888,
        "SYNTHETIC ONLY  |  PJM_RTO label  |  22-28 Jan 2025 (UTC)  |  Fixed seed 42",
        size=10,
        color="#7a887c",
    )
    top = fig.add_axes((0.09, 0.47, 0.86, 0.34))
    top.plot(
        times, [r["load_mw"] / 1000 for r in rows], color="#265c46", lw=1.6, label="Generated actual load"
    )
    top.plot(
        times,
        [r["forecast_mw"] / 1000 for r in rows],
        color="#a5b3a2",
        lw=1.2,
        ls="--",
        label="Supplied synthetic forecast",
    )
    flagged = [r for r in rows if r["detector_flag"]]
    injected = [r for r in rows if r["known_injection"]]
    top.scatter(
        [datetime.fromisoformat(r["timestamp_utc"].replace("Z", "+00:00")) for r in flagged],
        [r["load_mw"] / 1000 for r in flagged],
        marker="D",
        s=42,
        color="#c79c42",
        zorder=4,
        label=f"Detector flags ({len(flagged)})",
    )
    top.scatter(
        [datetime.fromisoformat(r["timestamp_utc"].replace("Z", "+00:00")) for r in injected],
        [r["load_mw"] / 1000 for r in injected],
        marker="o",
        s=70,
        facecolors="none",
        edgecolors="#353c35",
        linewidths=1.5,
        zorder=5,
        label="Known injected event (3h)",
    )
    top.set_ylabel("Hourly mean load (GW)")
    top.grid(axis="y")
    top.set_xlim(times[0], times[-1])
    top.set_ylim(50, max(r["load_mw"] for r in rows) / 1000 * 1.15)
    ticks = [times[0] + timedelta(days=i) for i in range(7)] + [times[-1]]
    top.set_xticks(ticks)
    top.xaxis.set_major_formatter(mdates.DateFormatter("%d Jan\n%H:%M", tz=UTC))
    top.legend(frameon=False, ncol=2, loc="upper right", fontsize=9)
    top.set_title("A  Hourly evidence and detector output", loc="left", pad=16, weight="bold")
    top.annotate(
        f"Known injected event: {demo['known_injected_flagged']}/{demo['known_injected_hours']} flagged",
        xy=(times[17], rows[17]["load_mw"] / 1000),
        xytext=(35, 10),
        textcoords="offset points",
        fontsize=9,
        color="#4a584b",
        arrowprops={"arrowstyle": "-", "color": "#7a887c", "lw": 0.7},
    )
    ax = fig.add_axes((0.09, 0.15, 0.52, 0.205))
    daily = demo["daily_mae"]
    bars = ax.bar(range(7), [r["mae_mw"] for r in daily], color="#628871", width=0.63)
    for bar, row in zip(bars, daily, strict=True):
        ax.annotate(
            f"{row['mae_mw']:,.0f}",
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            xytext=(0, 5),
            textcoords="offset points",
            ha="center",
            size=9,
        )
    ax.set_xticks(range(7), [r["day"][8:] + " Jan" for r in daily], size=9)
    ax.set_ylim(0, max(r["mae_mw"] for r in daily) * 1.32)
    ax.set_ylabel("MAE (MW)")
    ax.grid(axis="y")
    ax.set_title("B  Error of the supplied synthetic forecast", loc="left", pad=14, weight="bold")
    text = fig.add_axes((0.68, 0.15, 0.27, 0.205))
    text.axis("off")
    text.text(0, 0.93, "Observed failure case", weight="bold", size=11)
    for y, line in [
        (0.70, f"{demo['observed_hours']}/{demo['expected_hours']} observed hours"),
        (0.50, f"{demo['known_injected_flagged']}/{demo['known_injected_hours']} known event hours flagged"),
        (0.30, f"{demo['additional_flags']} other hours flagged for review"),
        (0.10, "Current threshold needs validation"),
    ]:
        text.text(0, y, line, size=10, color="#65796b")
    fig.text(
        0.09,
        0.065,
        "Actual and forecast values come from the demo generator; no forecasting model was trained. "
        "24 forecast pairs per day.",
        size=9,
        color="#7a887c",
    )
    fig.text(
        0.09,
        0.035,
        "Flags use causal, same-hour / day-type historical median-MAD scores. "
        "This example is not real-PJM detection accuracy.",
        size=9,
        color="#7a887c",
    )
    save_figure(fig, output, "energy-case-study")
    plt.close(fig)


def export_portfolio(report_path: Path, output: Path, tests_xml: Path | None = None):
    if not report_path.exists():
        raise DataError("评测文件不存在；先运行 energy-agent evaluate。不会自动触发付费模型评测。")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        summary, faults = review_report(report)
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"评测证据无法校验：{exc}") from exc
    output.mkdir(parents=True, exist_ok=True)
    tests, demo = read_tests(tests_xml), demo_evidence()
    evidence = {
        "benchmark": report["benchmark"],
        "planner": report["planner"],
        "model": report["model"],
        "summary": summary,
        "diagnostics": faults,
        "tests": tests,
        "demo": demo,
        "provenance": {
            "evaluation_path": report_path.name,
            "evaluation_sha256": digest(report_path),
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "generator_sha256": digest(Path(__file__).with_name("data.py")),
            "anomaly_code_sha256": digest(Path(__file__).with_name("anomaly.py")),
        },
        "limits": [
            "Curated synthetic tasks; not held-out real-PJM evidence.",
            "Recovery diagnostic uses an explicitly scripted planner, not an LLM.",
            "Runtime describes one local run and cannot establish a production SLA.",
            "Energy case uses an isolated synthetic database and exposes missed injected events.",
        ],
    }
    (output / "chart-values.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_csv(
        output / "latency-records.csv",
        [
            {
                "pipeline": r["mode"],
                "case": r["case"],
                "correct": r["correct"],
                "latency_ms": r["latency_ms"],
                "outcome": r["outcome"],
            }
            for r in report["records"]
        ],
    )
    write_csv(
        output / "fault-diagnostics.csv",
        [
            {
                "pipeline": r["mode"],
                "fault": r["fault"],
                "correct": r["correct"],
                "outcome": r["outcome"],
                "retries": r["retries"],
            }
            for r in report["semantic_diagnostics"]["records"]
        ],
    )
    write_csv(output / "energy-example.csv", demo["hourly"])
    plt = plotting()
    benchmark_figure(plt, output, evidence)
    business_figure(plt, output, demo)
    return {
        "output": str(output.resolve()),
        "figures": ["evaluation-overview.png", "energy-case-study.png"],
        "facts": {
            "synthetic_rows": demo["dataset_rows"],
            "tests": tests,
            "domain_faults_recovered": sum(faults["domain"].values()),
            "known_event_hours_flagged": demo["known_injected_flagged"],
            "known_event_hours": demo["known_injected_hours"],
        },
    }
