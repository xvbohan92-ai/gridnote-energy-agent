from datetime import UTC, datetime, timedelta

from energy_agent.agent import ask
from energy_agent.anomaly import score_history
from energy_agent.models import AnalysisTask, AskRequest, Scope


def daily_rows(days=18):
    rows = []
    for day in range(days):
        stamp = datetime(2025, 1, 1, 1, tzinfo=UTC) + timedelta(days=day)
        rows.append(
            {"timestamp_utc": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "load_mw": 100.0, "forecast_mw": 100.0}
        )
    return rows


def test_current_point_and_future_never_enter_its_baseline():
    raw = daily_rows()
    raw[14]["load_mw"] = 200.0
    scope = Scope(start="2025-01-15T00:00:00Z", end="2025-01-18T00:00:00Z", region="TEST")
    task = AnalysisTask(kind="anomaly", top_k=20)
    first = score_history(raw, scope, task)
    target = next(r for r in first["rows"] if r["timestamp_utc"] == raw[14]["timestamp_utc"])
    assert target["baseline_mw"] == 100.0
    assert target["robust_z"] == 100.0
    assert target["history_points"] == 10
    raw[15]["load_mw"] = 1e9  # Future point within the same requested output window.
    changed = score_history(list(reversed(raw)), scope, task)
    same = next(r for r in changed["rows"] if r["timestamp_utc"] == raw[14]["timestamp_utc"])
    assert same == target


def test_weekend_uses_weekend_peers():
    raw = daily_rows()
    for row in raw:
        stamp = datetime.fromisoformat(row["timestamp_utc"].replace("Z", "+00:00"))
        row["load_mw"] = 200.0 if stamp.weekday() >= 5 else 100.0
    result = score_history(
        raw,
        Scope(start="2025-01-18T00:00:00Z", end="2025-01-19T00:00:00Z", region="TEST"),
        AnalysisTask(kind="anomaly"),
    )
    assert result["rows"] == []
    assert result["anomaly_evidence"]["scored_hours"] == 1


def test_no_history_is_insufficient_not_normal(settings):
    result = ask(settings, AskRequest(question="2025-01-01 的负荷异常", region="DOM"))
    assert result["outcome"] == "insufficient_data"
    assert result["result"]["anomaly_evidence"]["insufficient_history_hours"] == 24
    assert result["result"]["anomaly_evidence"]["scored_hours"] == 0


def test_anomaly_output_never_exceeds_requested_window(settings):
    result = ask(settings, AskRequest(question="2025-01-22 的负荷异常", region="DOM"))
    assert result["result"]["anomaly_evidence"]["history_read_start"] < result["scope"]["start"]
    assert all(r["timestamp_utc"].startswith("2025-01-22") for r in result["result"]["rows"])
    assert result["sql_parameters"][0] == "DOM"
