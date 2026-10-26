"""Causal rolling robust baseline: never consume the current point or future observations."""

import statistics
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from .metrics import accepts_day, parse_utc, read_window
from .models import AnalysisTask, Scope

HISTORY_SQL = (
    "SELECT timestamp_utc, load_mw, forecast_mw FROM load_hourly "
    "WHERE region=? AND timestamp_utc>=? AND timestamp_utc<? ORDER BY timestamp_utc"
)


def score_history(raw, scope: Scope, task: AnalysisTask):
    queues = defaultdict(deque)
    found, eligible, insufficient = [], 0, 0
    start, end = parse_utc(scope.start), parse_utc(scope.end)
    # Sort defensively; callers cannot accidentally introduce future leakage through input ordering.
    for row in sorted(raw, key=lambda r: r["timestamp_utc"]):
        stamp = parse_utc(row["timestamp_utc"])
        if stamp >= end:
            continue
        peers = queues[(stamp.hour, stamp.weekday() < 5)]
        cutoff = stamp - timedelta(days=28)
        while peers and peers[0][0] < cutoff:
            peers.popleft()
        in_scope = start <= stamp < end and accepts_day(stamp, task.day_filter)
        if in_scope:
            if len(peers) < 4:
                insufficient += 1
            else:
                eligible += 1
                values = [point[1] for point in peers]
                median = statistics.median(values)
                mad = statistics.median(abs(value - median) for value in values)
                scale = max(1.4826 * mad, abs(median) * 0.01, 1.0)
                score = (row["load_mw"] - median) / scale
                if abs(score) >= 3.5:
                    found.append(
                        {
                            "timestamp_utc": row["timestamp_utc"],
                            "load_mw": row["load_mw"],
                            "baseline_mw": round(median, 3),
                            "mad_mw": round(mad, 3),
                            "robust_z": round(score, 3),
                            "history_points": len(peers),
                        }
                    )
        peers.append((stamp, row["load_mw"]))
    found.sort(key=lambda r: (-abs(r["robust_z"]), r["timestamp_utc"]))
    return {
        "columns": ["timestamp_utc", "load_mw", "baseline_mw", "mad_mw", "robust_z", "history_points"],
        "rows": found[: task.top_k],
        "truncated": False,
        "query_ms": 0,
        "anomaly_evidence": {
            "scored_hours": eligible,
            "insufficient_history_hours": insufficient,
            "flagged_hours": len(found),
            "shown_hours": min(len(found), task.top_k),
            "min_history_points": 4,
            "lookback_days": 28,
            "threshold": 3.5,
            "current_point_in_baseline": False,
            "future_points_in_baseline": False,
        },
    }


def detect_anomalies(path: Path, scope: Scope, task: AnalysisTask):
    started = time.perf_counter()
    history_start = (parse_utc(scope.start) - timedelta(days=28)).strftime("%Y-%m-%dT%H:%M:%SZ")
    history_scope = scope.model_copy(update={"start": history_start})
    result = score_history(read_window(path, history_scope), scope, task)
    result["anomaly_evidence"]["history_read_start"] = history_start
    result["anomaly_evidence"]["history_read_end_exclusive"] = scope.end
    result["sql_parameters"] = [scope.region, history_start, scope.end]
    result["query_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return result
