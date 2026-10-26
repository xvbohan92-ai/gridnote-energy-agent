import csv
import hashlib
from datetime import UTC, datetime

import pytest

from energy_agent.data import DataError, readonly
from energy_agent.eia import REQUIRED, eia_hour_start, import_eia, source_names


def write_sources(root, rows):
    for index, name in enumerate(source_names(2024)):
        with (root / name).open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=sorted(REQUIRED | {"Demand (MW) (Adjusted)"}))
            writer.writeheader()
            writer.writerows(rows.get(index, []))


def row(stamp, demand="10", forecast=""):
    return {
        "Balancing Authority": "PJM",
        "UTC Time at End of Hour": stamp,
        "Demand (MW)": demand,
        "Demand Forecast (MW)": forecast,
        "Demand (MW) (Adjusted)": "99999",
    }


def test_hour_ending_is_converted_in_utc_not_local_dst():
    assert eia_hour_start("03/10/2024 8:00:00 AM") == datetime(2024, 3, 10, 7, tzinfo=UTC)
    assert eia_hour_start("01/01/2025 12:00:00 AM") == datetime(2024, 12, 31, 23, tzinfo=UTC)


def test_import_crops_utc_year_and_preserves_nulls_and_source_hash(tmp_path):
    write_sources(
        tmp_path,
        {
            0: [row("01/01/2024 1:00:00 AM")],
            1: [row("07/01/2024 1:00:00 AM", "20", "21")],
            2: [row("01/01/2025 12:00:00 AM", "30"), row("01/01/2025 1:00:00 AM", "40")],
        },
    )
    db = tmp_path / "energy.sqlite"
    result = import_eia(tmp_path, db)
    audit = result["metadata"]["audit"]
    assert audit["rows"] == 3
    assert audit["expected_hours"] == 8784
    assert audit["forecast_rows"] == 1
    assert audit["outside_utc_year"] == 1
    with readonly(db) as conn:
        first = conn.execute("SELECT * FROM load_hourly ORDER BY timestamp_utc").fetchone()
        assert first["timestamp_utc"] == "2024-01-01T00:00:00Z"
        assert first["load_mw"] == 10  # Adjusted column must not be substituted.
        assert first["forecast_mw"] is None
    source = result["metadata"]["sources"][0]
    assert source["sha256"] == hashlib.sha256((tmp_path / source["file"]).read_bytes()).hexdigest()


def test_conflicting_duplicates_do_not_replace_existing_db(settings, tmp_path):
    before = settings.db_path.read_bytes()
    write_sources(tmp_path, {0: [row("01/01/2024 1:00:00 AM")], 1: [row("01/01/2024 1:00:00 AM", "11")]})
    with pytest.raises(DataError, match="冲突"):
        import_eia(tmp_path, settings.db_path)
    assert settings.db_path.read_bytes() == before


def test_identical_duplicate_is_audited_not_double_counted(tmp_path):
    item = row("01/01/2024 1:00:00 AM")
    write_sources(tmp_path, {0: [item], 1: [item]})
    result = import_eia(tmp_path, tmp_path / "energy.sqlite")
    assert result["metadata"]["audit"]["rows"] == 1
    assert result["metadata"]["audit"]["duplicate_identical_rows"] == 1


@pytest.mark.parametrize("value", ["-1", "NaN", "inf", "nonsense"])
def test_invalid_demand_aborts_instead_of_being_imputed(tmp_path, value):
    write_sources(tmp_path, {1: [row("01/01/2024 1:00:00 AM", value)]})
    with pytest.raises(DataError):
        import_eia(tmp_path, tmp_path / "energy.sqlite")
    assert not (tmp_path / "energy.sqlite").exists()
