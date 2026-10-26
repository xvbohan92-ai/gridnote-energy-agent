"""Reproducible demo data and transactional, strict CSV ingestion."""

import csv
import json
import math
import os
import random
import re
import sqlite3
import tempfile
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE load_hourly (
    timestamp_utc TEXT NOT NULL,
    region TEXT NOT NULL,
    load_mw REAL NOT NULL CHECK(load_mw >= 0),
    forecast_mw REAL CHECK(forecast_mw >= 0),
    PRIMARY KEY (timestamp_utc, region)
);
CREATE INDEX idx_region_time ON load_hourly(region, timestamp_utc);
CREATE TABLE dataset_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
ALIASES = {
    "timestamp_utc": ("timestamp_utc", "datetime_beginning_utc"),
    "region": ("region", "load_area"),
    "load_mw": ("load_mw", "mw"),
    "forecast_mw": ("forecast_mw",),
}


class DataError(ValueError):
    pass


def readonly(path: Path):
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _write_database(path: Path, rows, metadata: dict):
    """Build a new database and replace only after every input row is validated."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".energy-", suffix=".sqlite", dir=path.parent)
    os.close(fd)
    try:
        with closing(sqlite3.connect(temporary)) as conn, conn:
            conn.executescript(SCHEMA)
            try:
                conn.executemany("INSERT INTO load_hourly VALUES (?, ?, ?, ?)", rows)
            except sqlite3.IntegrityError as exc:
                raise DataError("重复的时间戳＋区域，或不合法的负荷值；原数据库未修改。") from exc
            count = conn.execute("SELECT COUNT(*) FROM load_hourly").fetchone()[0]
            if not count:
                raise DataError("CSV 没有数据行；原数据库未修改。")
            conn.executemany(
                "INSERT INTO dataset_meta VALUES (?, ?)",
                [(k, json.dumps(v, ensure_ascii=False)) for k, v in metadata.items()],
            )
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def create_demo(path: Path, *, replace: bool = False):
    if path.exists() and not replace:
        return
    rng = random.Random(42)
    start = datetime(2025, 1, 1, tzinfo=UTC)
    rows = []
    # Independent illustrative areas. PJM_RTO is not the sum of the other two.
    for hour in range(28 * 24):
        stamp = start + timedelta(hours=hour)
        for region, base in [("PJM_RTO", 85000), ("AECO", 1800), ("DOM", 13000)]:
            daily = 1 + 0.16 * math.sin((stamp.hour - 9) * math.pi / 12)
            weekly = 0.92 if stamp.weekday() >= 5 else 1
            seasonal = 1 + 0.07 * math.sin(hour * math.pi / (24 * 14))
            load = base * daily * weekly * seasonal + rng.gauss(0, base * 0.012)
            forecast = load * (1 + rng.gauss(0.006, 0.025))
            if stamp.day == 22 and 17 <= stamp.hour <= 19:
                load *= 1.25  # Known synthetic event; not a claimed real outage.
            rows.append((stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), region, round(load, 2), round(forecast, 2)))
    _write_database(
        path,
        rows,
        {
            "name": "Synthetic PJM-shaped hourly load",
            "source": "Deterministic synthetic generator, seed=42; NOT real PJM observations",
            "synthetic": True,
            "timezone": "UTC",
            "grain": "one hour × region",
            "created_at": datetime.now(UTC).isoformat(),
        },
    )


def parse_timestamp(value: str) -> str:
    text = value.strip()
    if not re.search(r"[T ]\d{2}:\d{2}", text):
        raise DataError("小时数据必须包含日期和时间，不能只提供日期。")
    try:
        # PJM's datetime_beginning_utc is explicitly UTC, including naive timestamps.
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    except ValueError as exc:
        raise DataError(f"时间戳必须为 ISO 8601 / UTC：{text[:80]}") from exc
    if stamp.minute or stamp.second or stamp.microsecond:
        raise DataError("只接受整点小时数据；请先聚合亚小时数据。")
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def import_csv(source: Path, target: Path, *, source_label: str):
    if not source_label.strip():
        raise DataError("必须提供数据来源说明。")

    def read_rows():
        with source.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            mapping = {k: next((a for a in aliases if a in fields), None) for k, aliases in ALIASES.items()}
            missing = [k for k in ("timestamp_utc", "region", "load_mw") if not mapping[k]]
            if missing:
                raise DataError("缺少字段：" + ", ".join(missing))
            for line, row in enumerate(reader, 2):
                try:
                    region = row[mapping["region"]].strip()
                    if not region or len(region) > 80:
                        raise DataError("区域不能为空或超过 80 字符。")
                    load = float(row[mapping["load_mw"]])
                    raw_forecast = row.get(mapping["forecast_mw"], "") or ""
                    forecast = float(raw_forecast) if raw_forecast.strip() else None
                    if not math.isfinite(load) or load < 0:
                        raise DataError("负荷必须为非负有限数。")
                    if forecast is not None and (not math.isfinite(forecast) or forecast < 0):
                        raise DataError("预测负荷必须为非负有限数或留空。")
                    yield (parse_timestamp(row[mapping["timestamp_utc"]]), region, load, forecast)
                except (ValueError, TypeError, AttributeError, KeyError) as exc:
                    raise DataError(f"CSV 第 {line} 行：{exc}") from exc

    _write_database(
        target,
        read_rows(),
        {
            "name": source.name,
            "source": source_label.strip(),
            "synthetic": False,
            "timezone": "UTC",
            "grain": "one hour × region",
            "created_at": datetime.now(UTC).isoformat(),
        },
    )


def dataset_context(path: Path) -> dict:
    with closing(readonly(path)) as conn:
        meta = {row[0]: json.loads(row[1]) for row in conn.execute("SELECT * FROM dataset_meta")}
        coverage = [
            dict(r)
            for r in conn.execute("""
            SELECT region, MIN(timestamp_utc) AS start, MAX(timestamp_utc) AS end,
                   COUNT(*) AS rows, COUNT(forecast_mw) AS forecast_rows,
                   ROUND(AVG(load_mw), 2) AS avg_load_mw, MAX(load_mw) AS peak_load_mw
            FROM load_hourly GROUP BY region ORDER BY region
        """)
        ]
        for item in coverage:
            span = datetime.fromisoformat(item["end"].replace("Z", "+00:00")) - datetime.fromisoformat(
                item["start"].replace("Z", "+00:00")
            )
            item["missing_hours"] = int(span.total_seconds() // 3600) + 1 - item["rows"]
    return {
        "metadata": meta,
        "regions": coverage,
        "schema": {
            "load_hourly": {
                "timestamp_utc": "UTC hour, ISO 8601 text YYYY-MM-DDTHH:MM:SSZ",
                "region": "area identifier; never sum an RTO total with component areas",
                "load_mw": "actual hourly average load, MW; energy MWh = sum(load_mw × 1h)",
                "forecast_mw": "optional forecast MW; NULL means unavailable, never treat as zero",
            }
        },
    }
