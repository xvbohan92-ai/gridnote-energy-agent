import pytest

from energy_agent.data import DataError, dataset_context, import_csv, parse_timestamp


def test_import_aliases_and_optional_forecast(tmp_path):
    csv = tmp_path / "input.csv"
    csv.write_text(
        "datetime_beginning_utc,load_area,mw\n2025-01-01 00:00:00,TEST,100\n2025-01-01 02:00:00,TEST,200\n"
    )
    db = tmp_path / "data.sqlite"
    import_csv(csv, db, source_label="test fixture")
    context = dataset_context(db)
    assert context["metadata"]["synthetic"] is False
    assert context["regions"][0]["missing_hours"] == 1
    assert context["regions"][0]["forecast_rows"] == 0
    assert context["regions"][0]["rows"] == 2


@pytest.mark.parametrize(
    "content",
    [
        "timestamp_utc,region,load_mw\n2025-01-01T00:00:00Z,X,10\n2025-01-01T00:00:00Z,X,20\n",
        "timestamp_utc,region,load_mw\n2025-01-01T00:00:00Z,X,nan\n",
        "timestamp_utc,region,load_mw\n2025-01-01T00:00:00Z,X,-10\n",
        "timestamp_utc,region,load_mw,forecast_mw\n2025-01-01T00:00:00Z,X,10,inf\n",
        "timestamp_utc,region,load_mw\n2025-01-01T00:30:00Z,X,10\n",
        "timestamp_utc,region,load_mw\n2025-01-01,X,10\n",
        "timestamp_utc,region,load_mw\n2025-01-01T00:00:00Z,,10\n",
        "timestamp_utc,region,load_mw\n",
        "date,region,load\n2025-01-01,X,10\n",
    ],
)
def test_invalid_import_preserves_original_database(settings, tmp_path, content):
    before = settings.db_path.read_bytes()
    csv = tmp_path / "bad.csv"
    csv.write_text(content)
    with pytest.raises(DataError):
        import_csv(csv, settings.db_path, source_label="invalid fixture")
    assert settings.db_path.read_bytes() == before
    assert not list(tmp_path.glob(".energy-*.sqlite"))


def test_offset_timestamps_normalized_to_utc():
    assert parse_timestamp("2025-01-01T01:00:00+01:00") == "2025-01-01T00:00:00Z"
