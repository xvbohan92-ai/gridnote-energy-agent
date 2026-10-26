import sqlite3

import pytest

from energy_agent.models import Scope
from energy_agent.sql import QueryError, execute_sql, guard_sql

SCOPE = Scope(start="2025-01-22T00:00:00Z", end="2025-01-29T00:00:00Z", region="DOM")


@pytest.mark.parametrize(
    "sql", ["SELECT 'unterminated FROM load_hourly", "SELECT * FROM load_hourly WHERE region='DOM"]
)
def test_unclosed_model_strings_are_rejected_as_query_errors(sql):
    with pytest.raises(QueryError, match="SQL 语法错误"):
        guard_sql(sql, SCOPE)


def test_all_source_references_are_scoped(settings):
    sql = """WITH all_rows AS (SELECT * FROM load_hourly WHERE 1=1 OR region='AECO')
             SELECT a.region, COUNT(*) AS n FROM all_rows a
             JOIN load_hourly b ON a.timestamp_utc=b.timestamp_utc GROUP BY a.region"""
    result = execute_sql(settings.db_path, guard_sql(sql, SCOPE))
    assert result["rows"] == [{"region": "DOM", "n": 168}]


@pytest.mark.parametrize(
    "sql",
    [
        "DROP TABLE load_hourly",
        "DELETE FROM load_hourly",
        "UPDATE load_hourly SET load_mw=0",
        "SELECT * FROM load_hourly; DELETE FROM load_hourly",
        "PRAGMA table_info(load_hourly)",
        "ATTACH DATABASE '/tmp/x' AS x",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM dataset_meta",
        "SELECT * FROM main.load_hourly",
        "SELECT * FROM readfile('/etc/passwd')",
        "WITH load_hourly AS (SELECT 1) SELECT * FROM load_hourly",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n) SELECT * FROM load_hourly,n",
        "SELECT * FROM load_hourly LIMIT -1",
        "SELECT * FROM load_hourly LIMIT 1+1",
    ],
)
def test_rejects_unsafe_or_unbounded_sql(sql):
    with pytest.raises(QueryError):
        guard_sql(sql, SCOPE)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT load_extension('/tmp/not-real') FROM load_hourly",
        "SELECT randomblob(1000000000) FROM load_hourly",
        "SELECT readfile('/etc/passwd') FROM load_hourly",
    ],
)
def test_authorizer_rejects_unapproved_functions(settings, sql):
    with pytest.raises(QueryError):
        execute_sql(settings.db_path, guard_sql(sql, SCOPE))


def test_readonly_connection_enforced_even_without_ast(settings):
    with pytest.raises(QueryError):
        execute_sql(settings.db_path, "DELETE FROM load_hourly")
    with sqlite3.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM load_hourly").fetchone()[0] == 2016


def test_truncation_is_reported(settings):
    scope = SCOPE.model_copy(update={"start": "2025-01-01T00:00:00Z"})
    result = execute_sql(
        settings.db_path, guard_sql("SELECT * FROM load_hourly ORDER BY timestamp_utc", scope)
    )
    assert len(result["rows"]) == 500
    assert result["truncated"] is True


def test_aggregation_uses_full_window_before_row_limit(settings):
    result = execute_sql(settings.db_path, guard_sql("SELECT COUNT(*) AS n FROM load_hourly", SCOPE))
    assert result["rows"] == [{"n": 168}]


def test_query_budget_stops_cartesian_explosion(settings):
    sql = guard_sql(
        "SELECT COUNT(*) FROM load_hourly a CROSS JOIN load_hourly b CROSS JOIN load_hourly c", SCOPE
    )
    with pytest.raises(QueryError, match="interrupted"):
        execute_sql(settings.db_path, sql)


def test_numeric_overflow_is_an_explicit_query_error(settings):
    with pytest.raises(QueryError, match="非有限"):
        execute_sql(settings.db_path, guard_sql("SELECT 1e999 AS overflow FROM load_hourly LIMIT 1", SCOPE))
