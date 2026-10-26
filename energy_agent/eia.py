"""EIA-930 reported demand adapter, preserving source snapshots and UTC hour semantics."""

import csv
import hashlib
import json
import math
import os
import tempfile
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .data import DataError, _write_database, dataset_context

BASE = "https://www.eia.gov/electricity/gridmonitor/sixMonthFiles/"
REQUIRED = {"Balancing Authority", "UTC Time at End of Hour", "Demand (MW)", "Demand Forecast (MW)"}


def source_names(year):
    if not 2016 <= year <= 2100:
        raise DataError("EIA 年份应为 2016–2100。")
    return [
        f"EIA930_BALANCE_{year - 1}_Jul_Dec.csv",
        f"EIA930_BALANCE_{year}_Jan_Jun.csv",
        f"EIA930_BALANCE_{year}_Jul_Dec.csv",
    ]


def download_sources(year, directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    for name in source_names(year):
        target = directory / name
        if target.exists():
            continue  # Existing snapshots are never silently refreshed.
        fd, temporary = tempfile.mkstemp(dir=directory, prefix=".eia-")
        try:
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(BASE + name, timeout=60) as source:
                size = 0
                while block := source.read(1024 * 1024):
                    size += len(block)
                    if size > 150 * 1024 * 1024:
                        raise DataError("EIA 单文件超过 150 MB，停止下载。")
                    out.write(block)
            with open(temporary, encoding="utf-8-sig", newline="") as stream:
                if not REQUIRED <= set(csv.DictReader(stream).fieldnames or []):
                    raise DataError("EIA 返回内容缺少要求的 CSV 字段。")
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def eia_hour_start(text):
    try:
        end = datetime.strptime(text.strip(), "%m/%d/%Y %I:%M:%S %p").replace(tzinfo=UTC)
    except ValueError as exc:
        raise DataError("EIA UTC Time at End of Hour 格式无效。") from exc
    if end.minute or end.second:
        raise DataError("EIA 时间必须对齐整点。")
    return end - timedelta(hours=1)


def number(text, *, optional=False):
    if not text.strip() and optional:
        return None
    try:
        value = float(text.replace(",", ""))
    except ValueError as exc:
        raise DataError("EIA 负荷必须为数字。") from exc
    if not math.isfinite(value) or value < 0:
        raise DataError("EIA 负荷必须为非负有限数。")
    return value


def import_eia(directory: Path, target: Path, *, year=2024, report: Path | None = None):
    """One PJM UTC year; no adjusted/imputed demand and no missing-hour zero fill."""
    start, end = datetime(year, 1, 1, tzinfo=UTC), datetime(year + 1, 1, 1, tzinfo=UTC)
    selected, sources = {}, []
    stats = {
        "source_pjm_rows": 0,
        "outside_utc_year": 0,
        "missing_demand_rows": 0,
        "duplicate_identical_rows": 0,
    }
    for name in source_names(year):
        path = directory / name
        if not path.is_file():
            raise DataError(f"缺少 EIA 源文件 {name}；先使用 --download 或提供本地文件。")
        sources.append(
            {
                "url": BASE + name,
                "file": name,
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if not REQUIRED <= set(reader.fieldnames or []):
                raise DataError(f"{name} 缺少 EIA-930 字段。")
            for line, row in enumerate(reader, 2):
                if row["Balancing Authority"] != "PJM":
                    continue
                stats["source_pjm_rows"] += 1
                try:
                    stamp = eia_hour_start(row["UTC Time at End of Hour"])
                    if not start <= stamp < end:
                        stats["outside_utc_year"] += 1
                        continue
                    if not row["Demand (MW)"].strip():
                        stats["missing_demand_rows"] += 1
                        continue
                    key = stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
                    item = (
                        key,
                        "PJM",
                        number(row["Demand (MW)"]),
                        number(row["Demand Forecast (MW)"], optional=True),
                    )
                    if key in selected:
                        if selected[key] != item:
                            raise DataError("同一 UTC 小时存在冲突值；停止导入，原数据库不变。")
                        stats["duplicate_identical_rows"] += 1
                    selected[key] = item
                except (DataError, KeyError, AttributeError) as exc:
                    raise DataError(f"{name} 第 {line} 行：{exc}") from exc
    rows = sorted(selected.values())
    expected = int((end - start).total_seconds() // 3600)
    missing = [
        (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in range(expected)
        if (start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ") not in selected
    ]
    fingerprint = hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()
    audit = {
        "year": year,
        "region": "PJM",
        "rows": len(rows),
        "expected_hours": expected,
        "missing_hours": len(missing),
        "missing_hour_starts": missing,
        "forecast_rows": sum(r[3] is not None for r in rows),
        "row_sha256": fingerprint,
        **stats,
    }
    metadata = {
        "name": f"EIA-930 PJM reported hourly demand · {year}",
        "synthetic": False,
        "source": "U.S. Energy Information Administration, EIA-930 historical balance CSV snapshots",
        "source_kind": "eia930",
        "timezone": "UTC",
        "grain": "one hour × region",
        "created_at": datetime.now(UTC).isoformat(),
        "sources": sources,
        "audit": audit,
        "time_conversion": "UTC Time at End of Hour minus exactly one hour; UTC calendar-year crop",
        "value_selection": "Demand (MW) and Demand Forecast (MW); adjusted/imputed columns unused",
        "forecast_limitation": "Reported forecast snapshot; not an as-of backtest or a trained model",
        "attribution": "Source: U.S. Energy Information Administration; normalized "
        + datetime.now(UTC).date().isoformat()
        + ". Historical files may be revised; SHA-256 identifies the actual snapshots.",
    }
    _write_database(target, rows, metadata)
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return dataset_context(target)
