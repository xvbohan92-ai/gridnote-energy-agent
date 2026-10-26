import math
import sqlite3
import time
from typing import TypedDict

import sqlglot
from langgraph.graph import END, START, StateGraph

from .anomaly import HISTORY_SQL, detect_anomalies
from .config import Settings
from .data import dataset_context
from .errors import PlanningError, SemanticError, failure_outcome
from .metrics import quality, read_window, verify_result
from .models import AnalysisTask, AskRequest, QueryPlan, Scope
from .planner import DemoPlanner, MLXPlanner, OpenAIPlanner
from .scope import ScopeError, resolve_scope
from .sql import QueryError, execute_sql, guard_sql
from .tasks import compile_task, parse_task


class AgentState(TypedDict, total=False):
    request: AskRequest
    context: dict
    scope: Scope
    task: AnalysisTask
    plan: QueryPlan
    sql: str
    error: str | None
    error_code: str | None
    retries: int
    trace: list[dict]
    result: dict
    semantic_check: dict
    quality: dict
    response: dict


def empty_result():
    return {"columns": [], "rows": [], "truncated": False, "query_ms": 0}


def build_agent(settings: Settings, planner, *, baseline=False, mode=None):
    pipeline = mode or ("baseline" if baseline else "domain")
    if pipeline not in ("baseline", "agent", "domain"):
        raise ValueError("Unknown pipeline")

    def event(state, node, status, detail, started):
        return state.get("trace", []) + [
            {
                "node": node,
                "status": status,
                "detail": detail,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        ]

    def failed(state, node, error, started):
        return {
            "error": str(error),
            "error_code": error.code,
            "result": empty_result(),
            "trace": event(state, node, "error", str(error), started),
        }

    def inspect(state):
        started = time.perf_counter()
        context = dataset_context(settings.db_path)
        try:
            scope = resolve_scope(state["request"], context)
            task = parse_task(state["request"].question)
            contract = task.model_dump()
            if task.kind not in ("custom", "anomaly"):
                contract["required_columns"] = sqlglot.parse_one(
                    compile_task(task).sql, read="sqlite"
                ).named_selects
            context["task_contract"] = contract
            return {
                "context": context,
                "scope": scope,
                "task": task,
                "retries": 0,
                "error": None,
                "error_code": None,
                "trace": event(
                    state,
                    "inspect",
                    "ok",
                    f"{scope.region} · {scope.start[:10]} → "
                    f"{scope.end[:10]} (结束不含) · {task.kind}/{task.granularity}",
                    started,
                ),
            }
        except (ScopeError, PlanningError) as exc:
            return {"context": context, "retries": 0, **failed(state, "inspect", exc, started)}

    def plan(state):
        started = time.perf_counter()
        previous = (
            {"sql": state["plan"].sql, "error": state["error"], "error_code": state["error_code"]}
            if state.get("plan") and state.get("error")
            else None
        )
        try:
            task = state["task"]
            # The causal anomaly detector is a bounded local analytical tool in all three pipelines.
            result = (
                compile_task(task)
                if task.kind == "anomaly"
                else planner.plan(state["request"].question, state["context"], state["scope"], previous)
            )
            return {
                "plan": result,
                "error": None,
                "error_code": None,
                "trace": event(state, "plan", "ok", result.title, started),
            }
        except PlanningError as exc:
            return failed(state, "plan", exc, started)

    def check(state):
        started = time.perf_counter()
        try:
            if state["task"].kind == "anomaly":
                return {
                    "sql": HISTORY_SQL,
                    "error": None,
                    "error_code": None,
                    "trace": event(
                        state, "check", "ok", "调用受限历史基线工具；只向用户返回请求窗口内的异常", started
                    ),
                }
            sql = guard_sql(state["plan"].sql, state["scope"], settings.max_rows)
            if pipeline != "baseline":
                execute_sql(
                    settings.db_path,
                    "EXPLAIN QUERY PLAN " + sql,
                    timeout=settings.query_timeout,
                    max_rows=settings.max_rows,
                )
            return {
                "sql": sql,
                "error": None,
                "error_code": None,
                "trace": event(
                    state,
                    "check",
                    "ok",
                    "只读与范围限制通过" + ("；预编译通过" if pipeline != "baseline" else "；基线不重试"),
                    started,
                ),
            }
        except QueryError as exc:
            return failed(state, "check", exc, started)

    def execute(state):
        started = time.perf_counter()
        try:
            if state["task"].kind == "anomaly":
                result = detect_anomalies(settings.db_path, state["scope"], state["task"])
            else:
                result = execute_sql(
                    settings.db_path, state["sql"], timeout=settings.query_timeout, max_rows=settings.max_rows
                )
            return {
                "result": result,
                "error": None,
                "error_code": None,
                "trace": event(state, "execute", "ok", f"返回 {len(result['rows'])} 行", started),
            }
        except QueryError as exc:
            return failed(state, "execute", exc, started)

    def verify(state):
        started = time.perf_counter()
        raw = read_window(settings.db_path, state["scope"])
        coverage = quality(raw, state["scope"], state["task"])
        try:
            if state["task"].kind == "anomaly":
                checked = {
                    "status": "domain_tool",
                    "checks": ["仅过去的历史点", "历史不足单独计数", "UTC 日类型匹配"],
                }
            elif pipeline == "domain":
                checked = verify_result(raw, state["task"], state["result"], settings.max_rows)
            else:
                checked = {"status": "not_run", "checks": [], "limitation": "对照流程不进行领域数值核验。"}
            return {
                "quality": coverage,
                "semantic_check": checked,
                "error": None,
                "error_code": None,
                "trace": event(
                    state, "semantics", "ok", "独立 Python 指标核验：" + checked["status"], started
                ),
            }
        except SemanticError as exc:
            return {
                "quality": coverage,
                "semantic_check": {"status": "failed", "checks": []},
                **failed(state, "semantics", exc, started),
            }

    def repair(state):
        return {
            "retries": state["retries"] + 1,
            "trace": state["trace"]
            + [
                {
                    "node": "repair",
                    "status": "retry",
                    "duration_ms": 0,
                    "detail": f"基于 {state['error_code']} 重写，第 {state['retries'] + 1} 次",
                }
            ],
        }

    def finish(state):
        started = time.perf_counter()
        task, plan = state.get("task"), state.get("plan")
        if task and plan and state.get("semantic_check", {}).get("status") == "verified":
            canonical = compile_task(task)
            plan = plan.model_copy(
                update={key: getattr(canonical, key) for key in ("title", "chart", "x", "y", "note")}
            )
        result = state.get("result", empty_result())
        rows, warnings = result["rows"], []
        metadata = state["context"]["metadata"]
        if metadata["synthetic"]:
            warnings.append("当前为合成演示数据，不代表真实 PJM 运行情况。")
        coverage = state.get("quality")
        if coverage:
            if coverage["missing_hours"]:
                warnings.append(
                    f"请求窗口缺少 {coverage['missing_hours']} 个小时，"
                    f"覆盖 {coverage['coverage_pct']}%；未补零。"
                )
            if coverage["forecast_hours"] < coverage["observed_hours"]:
                warnings.append(
                    "请求窗口存在缺失预测；误差仅覆盖预测可用小时，MAPE 还排除实际负荷为零的小时。"
                )
        if result["truncated"]:
            warnings.append(f"结果超过 {settings.max_rows} 行，仅展示前 {settings.max_rows} 行，请先聚合。")
        if rows and "mse_mw2" in result["columns"] and "rmse_mw" not in result["columns"]:
            for row in rows:
                value = row["mse_mw2"]
                row["rmse_mw"] = round(math.sqrt(max(0, value)), 3) if value is not None else None
            result["columns"] = result["columns"] + ["rmse_mw"]
        anomaly = result.get("anomaly_evidence")
        if state.get("error"):
            status, outcome, answer = "error", failure_outcome(state["error_code"]), state["error"]
        elif anomaly and anomaly["scored_hours"] == 0:
            status, outcome = "insufficient_data", "insufficient_data"
            answer = "没有达到最小历史样本数的小时，无法判断异常；历史不足不代表正常。"
        elif not rows:
            status, outcome, answer = (
                "empty",
                "empty",
                "所选范围没有符合条件的结果；未扩大结果范围或更换区域。",
            )
        else:
            status, outcome = "ok", "success"
            answer = f"查询返回 {len(rows)} 行" + ("（展示上限）" if result["truncated"] else "") + "。"
            if task and task.kind == "forecast":
                valid = [r for r in rows if isinstance(r.get("mae_mw"), (int, float))]
                if valid:
                    worst = max(valid, key=lambda r: r["mae_mw"])
                    label = next(
                        (worst[k] for k in ("day", "week", "timestamp_utc", "day_type") if k in worst), "整体"
                    )
                    answer += f" 返回分组中最高 MAE 为 {worst['mae_mw']:,.3f} MW（{label}）。"
                else:
                    answer = "所选范围没有可用预测值，无法计算 MAE、RMSE 和 MAPE。"
                    status, outcome = "insufficient_data", "insufficient_data"
            elif task and task.kind == "energy":
                total = sum(r["energy_mwh"] for r in rows if isinstance(r.get("energy_mwh"), (int, float)))
                answer += f" 已展示分组的观测电量为 {total:,.2f} MWh（未补齐缺失小时）。"
            elif anomaly:
                answer += f" 已评分 {anomaly['scored_hours']} 小时，标记 {anomaly['flagged_hours']} 小时；"
                answer += "仅为筛查，不解释原因。"
            elif task and task.kind in ("peak", "trend"):
                valid = [r for r in rows if isinstance(r.get("load_mw"), (int, float))]
                if valid:
                    peak = max(valid, key=lambda r: r["load_mw"])
                    term = "小时平均负荷" if task.granularity == "hour" else "分组平均负荷"
                    answer += f" 返回结果中最高{term}为 {peak['load_mw']:,.2f} MW。"
        if anomaly and anomaly["insufficient_history_hours"]:
            warnings.append(
                f"{anomaly['insufficient_history_hours']} 个小时因历史样本少于 4 个而未评分；不视为正常。"
            )
        if anomaly:
            warnings.append("历史基线会读取请求起点前最多 28 天；每点只用其过去的数据，输出仍限于请求窗口。")
        chart = None
        if rows and plan and plan.chart != "none" and plan.x in result["columns"]:
            ys = [
                y
                for y in plan.y
                if y in result["columns"] and any(isinstance(r.get(y), (int, float)) for r in rows)
            ]
            if ys:
                chart = {
                    "type": plan.chart,
                    "x": plan.x,
                    "y": ys,
                    "data": rows,
                    "unit": task.unit if task else "",
                }
        if state.get("scope"):
            warnings.append(state["scope"].note)
        # Display trusted definitions after a known task has been numerically verified.
        checked = state.get("semantic_check", {"status": "not_run", "checks": []})
        method = (
            compile_task(task).note
            if task and checked["status"] in ("verified", "domain_tool")
            else plan.note
            if plan
            else ""
        )
        return {
            "response": {
                "status": status,
                "outcome": outcome,
                "error_code": state.get("error_code"),
                "answer": answer,
                "title": plan.title if plan else "查询未完成",
                "question": state["request"].question,
                "planner": planner.name,
                "model": getattr(planner, "model", None),
                "pipeline": pipeline,
                "scope": state["scope"].model_dump() if state.get("scope") else None,
                "task": task.model_dump() if task else None,
                "sql": state.get("sql", ""),
                "sql_parameters": result.get("sql_parameters"),
                "generated_sql": plan.sql if plan else "",
                "method": method,
                "result": result,
                "chart": chart,
                "warnings": warnings,
                "semantic_check": checked,
                "quality": coverage,
                "source": metadata,
                "retries": state.get("retries", 0),
                "usage": planner.usage,
                "trace": event(state, "summarize", status, "根据返回数据与校验状态生成摘要", started),
            }
        }

    def route_failure(state):
        return "repair" if pipeline != "baseline" and state["retries"] < settings.max_retries else "finish"

    graph = StateGraph(AgentState)
    for name, fn in [
        ("inspect", inspect),
        ("plan", plan),
        ("check", check),
        ("execute", execute),
        ("verify", verify),
        ("repair", repair),
        ("finish", finish),
    ]:
        graph.add_node(name, fn)
    graph.add_edge(START, "inspect")
    graph.add_conditional_edges("inspect", lambda s: "finish" if s.get("error") else "plan")
    graph.add_conditional_edges("plan", lambda s: "finish" if s.get("error") else "check")
    graph.add_conditional_edges("check", lambda s: route_failure(s) if s.get("error") else "execute")
    graph.add_conditional_edges("execute", lambda s: route_failure(s) if s.get("error") else "verify")
    graph.add_conditional_edges("verify", lambda s: route_failure(s) if s.get("error") else "finish")
    graph.add_edge("repair", "plan")
    graph.add_edge("finish", END)
    return graph.compile()


def ask(settings: Settings, request: AskRequest, *, planner=None, baseline=False, mode=None) -> dict:
    if planner is None and settings.planner in ("openai-tools", "ollama-tools"):
        from .tool_agent import ask_tool_agent

        try:
            return ask_tool_agent(settings, request, mode=mode or ("baseline" if baseline else "domain"))
        except (sqlite3.Error, OSError) as exc:
            raise PlanningError("数据库读取失败，请检查服务日志。", code="data_error") from exc
    started = time.perf_counter()
    owned = planner is None
    if owned:
        if settings.planner == "openai":
            planner = OpenAIPlanner(settings.model)
        elif settings.planner == "mlx-sql":
            planner = MLXPlanner(settings.mlx_base_url, settings.model)
        elif settings.planner == "demo":
            planner = DemoPlanner()
        else:
            raise PlanningError("ENERGY_PLANNER 只支持 demo 或 openai。", code="configuration_error")
    try:
        response = build_agent(settings, planner, baseline=baseline, mode=mode).invoke({"request": request})[
            "response"
        ]
        response["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
        if settings.planner == "mlx-sql" and owned:
            response.update(provider="mlx", engine="langgraph-mlx-sql",
                            adapter_sha256=planner.adapter_sha256)
        return response
    except (sqlite3.Error, OSError) as exc:
        raise PlanningError("数据库读取失败，请检查服务日志。", code="data_error") from exc
    finally:
        if owned and isinstance(planner, (OpenAIPlanner, MLXPlanner)):
            planner.client.close()
