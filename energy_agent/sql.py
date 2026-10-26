"""AST validation, enforced scope, SQLite authorizer, bounded execution."""

import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError, TokenError

from .data import readonly
from .errors import AgentError
from .models import Scope


class QueryError(AgentError):
    default_code = "query_error"


FUNCTIONS = {
    "abs",
    "avg",
    "count",
    "max",
    "min",
    "sum",
    "total",
    "round",
    "coalesce",
    "nullif",
    "strftime",
    "date",
    "datetime",
    "substr",
    "substring",
    "cast",
    "ifnull",
    "lower",
    "upper",
    "length",
    "like",
    "glob",
    "row_number",
    "rank",
    "dense_rank",
    "lag",
    "lead",
}


def guard_sql(sql: str, scope: Scope, max_rows: int = 500) -> str:
    if not sql.strip() or len(sql) > 16000:
        raise QueryError("SQL 为空或过长。")
    try:
        statements = sqlglot.parse(sql, read="sqlite")
    except (ParseError, TokenError) as exc:
        raise QueryError(f"SQL 语法错误：{str(exc)[:250]}") from exc
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise QueryError("只允许一条 SELECT / WITH 查询。")
    tree = statements[0]
    # Reject any mutation nested inside a query, including data-changing CTEs.
    forbidden = (exp.DDL, exp.DML, exp.Command, exp.Into, exp.Attach, exp.Pragma, exp.Transaction)
    if any(isinstance(node, forbidden) for node in tree.walk()):
        raise QueryError("查询包含写操作或管理命令。")
    if any(cte.args.get("recursive") for cte in tree.find_all(exp.With)):
        raise QueryError("不允许递归 CTE。")
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    if "load_hourly" in ctes:
        raise QueryError("CTE 不得覆盖源表名。")
    source_tables = []
    for table in tree.find_all(exp.Table):
        if table.db or table.catalog or not isinstance(table.this, exp.Identifier):
            raise QueryError("不允许跨库访问或表函数。")
        name = table.name.lower()
        if name == "load_hourly":
            source_tables.append(table)
        elif name not in ctes:
            raise QueryError(f"只允许读取 load_hourly；禁止访问 {table.name}。")
    if not source_tables:
        raise QueryError("查询必须读取 load_hourly。")
    # Scope is applied to EVERY reference before aggregation/window functions; model text cannot bypass it.
    predicate = exp.and_(
        exp.GTE(this=exp.column("timestamp_utc"), expression=exp.Literal.string(scope.start)),
        exp.LT(this=exp.column("timestamp_utc"), expression=exp.Literal.string(scope.end)),
        exp.EQ(this=exp.column("region"), expression=exp.Literal.string(scope.region)),
    )
    for table in source_tables:
        bounded = exp.select("*").from_("load_hourly").where(predicate.copy())
        table.replace(bounded.subquery(alias=table.alias_or_name))
    # Keep smaller model limits. Fetch one extra row to identify truncation honestly.
    existing = tree.args.get("limit")
    try:
        limit = int(existing.expression.this) if existing else max_rows + 1
    except (ValueError, TypeError, AttributeError) as exc:
        raise QueryError("LIMIT 必须是正整数常量。") from exc
    if limit < 1:
        raise QueryError("LIMIT 必须为正数。")
    tree = tree.limit(min(limit, max_rows + 1), copy=False)
    return tree.sql(dialect="sqlite")


def _authorize(action, arg1, arg2, database, trigger):
    if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_READ and arg1 == "load_hourly" and database == "main":
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def execute_sql(path: Path, sql: str, *, timeout: float = 2.0, max_rows: int = 500) -> dict:
    started = time.perf_counter()
    steps = 0

    def progress():
        nonlocal steps
        steps += 1000
        return int(time.perf_counter() - started > timeout or steps > 2_000_000)

    try:
        with closing(readonly(path)) as conn:
            conn.set_authorizer(_authorize)
            conn.set_progress_handler(progress, 1000)
            cursor = conn.execute(sql)
            columns = [d[0] for d in cursor.description]
            if len({column.lower() for column in columns}) != len(columns):
                raise QueryError("结果列名重复，请为每一列指定唯一别名。")
            values = cursor.fetchmany(max_rows + 1)
            if any(isinstance(value, float) and not math.isfinite(value) for row in values for value in row):
                raise QueryError("查询产生了非有限数值，请检查计算溢出或缩小范围。")
            return {
                "columns": columns,
                "rows": [dict(r) for r in values[:max_rows]],
                "truncated": len(values) > max_rows,
                "query_ms": round((time.perf_counter() - started) * 1000, 2),
            }
    except sqlite3.Error as exc:
        raise QueryError(f"SQL 执行失败：{exc}") from exc
