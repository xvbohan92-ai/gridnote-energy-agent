import csv
import hashlib
import json

import pytest

from energy_agent.data import DataError
from energy_agent.real_validation import expected_rows, matches, source_oracle


def test_oracle_decodes_source_without_production_importer(tmp_path):
    path = tmp_path / "source.csv"
    with path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            ["Balancing Authority", "UTC Time at End of Hour", "Demand (MW)", "Demand Forecast (MW)"]
        )
        writer.writerow(["PJM", "01/01/2024 1:00:00 AM", "10", "12"])
        writer.writerow(["PJM", "01/01/2024 2:00:00 AM", "20", ""])
        writer.writerow(["PJM", "01/01/2025 1:00:00 AM", "99", "99"])
    entries = [{"file": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}]
    raw = source_oracle(tmp_path, entries, 2024)
    assert len(raw) == 2
    case = {"start": "2024-01-01", "end": "2024-01-01", "kind": "forecast", "granularity": "total"}
    assert expected_rows(raw, case) == [
        {
            "observed_hours": 2,
            "forecast_hours": 1,
            "mae_mw": 2.0,
            "mse_mw2": 4.0,
            "mape_pct": 20.0,
            "mape_hours": 1,
        }
    ]
    energy = expected_rows(raw, {**case, "kind": "energy"})
    assert energy == [{"observed_hours": 2, "energy_mwh": 30.0}]
    path.write_text("changed snapshot")
    with pytest.raises(DataError, match="SHA-256"):
        source_oracle(tmp_path, entries, 2024)


def test_scores_order_counts_and_nulls_not_only_success_status():
    assert matches([{"x": 1, "y": None}], [{"x": 1.001, "y": None}])
    assert not matches([{"x": 1, "y": None}], [{"x": 1, "y": 0}])
    assert not matches([{"x": 1}, {"x": 2}], [{"x": 2}, {"x": 1}])
    assert not matches([{"x": 1}], [])


def test_pilot_time_windows_are_separate_and_authorship_is_disclosed():
    from energy_agent.real_validation import CASES

    fixture = json.loads(CASES.read_text())
    assert "no independent human review" in fixture["authorship"]
    cases = fixture["cases"]
    assert len({c["id"] for c in cases}) == len(cases)
    dev = [c for c in cases if c["split"] == "dev"]
    test = [c for c in cases if c["split"] == "test"]
    assert max(c["end"] for c in dev) < min(c["start"] for c in test)
