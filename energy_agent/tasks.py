"""Extract supported task contracts and compile their trusted SQL definitions."""

import re

from .errors import PlanningError
from .models import AnalysisTask, QueryPlan


def parse_task(question: str) -> AnalysisTask:
    q = question.lower()
    if re.search(r"删除|修改|\b(?:drop|delete|update|insert|pragma|attach)\b", q):
        raise PlanningError("此助手只提供只读分析。", code="policy_refusal")
    if re.search(r"电价|价格|天气|碳排|\b(?:price|weather|carbon)\b", q):
        raise PlanningError("当前数据没有电价、天气或碳排放，无法支持该问题。", code="data_unavailable")
    if re.search(r"异常|anomal|outlier", q):
        kind = "anomaly"
    elif re.search(r"误差|预测|forecast|mae|mape|rmse", q):
        kind = "forecast"
    elif re.search(r"峰值|最高|最大|peak|\bmax\b|highest|top\s*\d", q):
        kind = "peak"
    elif re.search(r"电量|mwh|energy|用电量", q):
        kind = "energy"
    elif re.search(r"记录数|多少条|\bcount\b|number of (?:rows|records)", q):
        kind = "count"
    elif re.search(r"趋势|负荷|trend|load|daily", q):
        kind = "trend"
    else:
        kind = "custom"

    # Do not certify a simple mean when the wording requests an unsupported statistic.
    if re.search(
        r"相关|关联|因果|原因|归因|同比|环比|增长率|correl|cause|\bwhy\b|percent change|"
        r"中位数|分位数|标准差|方差|median|percentile|variance|差值|最低|最小|\bmin\b|lowest",
        q,
    ):
        kind = "custom"
    if kind == "forecast" and re.search(r"最大|最高|top\s*\d|highest", q):
        kind = "custom"
    if kind == "peak" and re.search(r"每日|每天|日均|daily|per day", q):
        kind = "custom"

    top = re.search(r"(?:前|top\s*|最高的?\s*|最大的?\s*|highest\s+)([-+]?\d+(?:\.\d+)?)", q)
    if top and not top[1].isdecimal():
        raise PlanningError("Top-K 必须是 1–100 之间的整数。", code="unsupported_question")
    top_k = int(top[1]) if top else (20 if kind == "anomaly" else 5)
    if not 1 <= top_k <= 100:
        raise PlanningError("Top-K 应在 1–100 之间。", code="unsupported_question")
    granularity = "day"
    if re.search(r"每小时|逐小时|按小时|hourly|per hour", q):
        granularity = "hour"
    elif re.search(r"每周|按周|周均|weekly|per week", q):
        granularity = "week"
    elif re.search(r"总计|合计|总用电量|总电量|整体|总体|\btotal\b|overall", q):
        granularity = "total"
    weekdays = bool(re.search(r"工作日|weekday", q))
    weekends = bool(re.search(r"周末|weekend", q))
    day_filter = "all"
    if weekdays and weekends:
        if kind in ("peak", "anomaly"):
            raise PlanningError("峰值和异常筛查请分别查询工作日与周末。", code="unsupported_question")
        granularity = "day_type"
    elif weekdays:
        day_filter = "weekday"
    elif weekends:
        day_filter = "weekend"
    if kind == "peak":
        if granularity not in ("day", "hour"):
            raise PlanningError(
                "峰值任务目前按原始小时排序；请明确查询小时峰值。", code="unsupported_question"
            )
        granularity = "hour"
    if kind in ("count", "anomaly"):
        granularity = "total" if kind == "count" else "hour"
    return AnalysisTask(
        kind=kind,
        granularity=granularity,
        top_k=top_k,
        day_filter=day_filter,
        unit="MWh"
        if kind == "energy"
        else "robust z"
        if kind == "anomaly"
        else "rows"
        if kind == "count"
        else "单位未核验"
        if kind == "custom"
        else "MW",
    )


DAY_TYPE = (
    "CASE WHEN CAST(STRFTIME('%w', timestamp_utc) AS INTEGER) IN (0, 6) THEN 'weekend' ELSE 'weekday' END"
)
PERIODS = {
    "hour": ("timestamp_utc", "timestamp_utc"),
    "day": ("SUBSTR(timestamp_utc, 1, 10)", "day"),
    "week": (
        "DATE(timestamp_utc, '-' || ((CAST(STRFTIME('%w', timestamp_utc) AS INTEGER) + 6) % 7) || ' days')",
        "week",
    ),
    "day_type": (DAY_TYPE, "day_type"),
}


def day_predicate(task: AnalysisTask) -> str:
    if task.day_filter == "weekday":
        return "CAST(STRFTIME('%w', timestamp_utc) AS INTEGER) NOT IN (0, 6)"
    if task.day_filter == "weekend":
        return "CAST(STRFTIME('%w', timestamp_utc) AS INTEGER) IN (0, 6)"
    return "1=1"


def compile_task(task: AnalysisTask) -> QueryPlan:
    """Known metric definitions are a contract, not a claim of general NL understanding."""
    if task.kind == "custom":
        raise PlanningError("规则模式不支持该问题；开放式查询请配置模型。", code="unsupported_question")
    if task.kind == "anomaly":
        return QueryPlan(
            title="历史基线异常筛查",
            sql="",
            chart="bar",
            x="timestamp_utc",
            y=["robust_z"],
            note="只使用当前时点之前 28 天、相同 UTC 小时及工作日/周末类型的负荷。"
            "至少 4 个历史点；基线为中位数，尺度为 max(1.4826×MAD, |中位数|×1%, 1MW)。"
            "绝对稳健分数 ≥3.5 时标记异常；历史不足标记为不可评分，不视为正常。",
        )
    where = f" FROM load_hourly WHERE {day_predicate(task)}"
    if task.kind == "count":
        return QueryPlan(
            title="负荷记录数",
            sql="SELECT COUNT(*) AS n" + where,
            chart="none",
            x="",
            y=[],
            note="统计所选范围、区域和日类型中的实际记录数。",
        )
    if task.kind == "peak":
        return QueryPlan(
            title=f"负荷峰值 Top {task.top_k}",
            sql="SELECT timestamp_utc, load_mw, forecast_mw"
            + where
            + f" ORDER BY load_mw DESC, timestamp_utc LIMIT {task.top_k}",
            chart="bar",
            x="timestamp_utc",
            y=["load_mw"],
            note=f"按实际小时平均负荷降序取前 {task.top_k} 条，相同时按 UTC 时间升序。",
        )
    period, key = PERIODS.get(task.granularity, (None, ""))
    prefix = f"{period} AS {key}, " if period else ""
    suffix = f" GROUP BY {key} ORDER BY {key}" if period else ""
    count = "COUNT(*) AS observed_hours, COUNT(forecast_mw) AS forecast_hours"
    if task.kind == "forecast":
        expressions = (
            count
            + """, COUNT(CASE WHEN forecast_mw IS NOT NULL AND load_mw > 0 THEN 1 END) AS mape_hours,
            ROUND(AVG(ABS(forecast_mw - load_mw)), 3) AS mae_mw,
            ROUND(AVG((forecast_mw - load_mw) * (forecast_mw - load_mw)), 3) AS mse_mw2,
            ROUND(AVG(CASE WHEN load_mw > 0 AND forecast_mw IS NOT NULL
                THEN 100.0 * ABS(forecast_mw - load_mw) / load_mw END), 3) AS mape_pct"""
        )
        title, ys = "预测误差", ["mae_mw"]
        note = "MAE/MSE 只覆盖预测非空小时，MAPE 还排除实际负荷为零的小时；分别返回分母。RMSE=√MSE。"
    elif task.kind == "energy":
        expressions = "COUNT(*) AS observed_hours, ROUND(SUM(load_mw), 2) AS energy_mwh"
        title, ys = "观测电量", ["energy_mwh"]
        note = "每行代表 1 小时平均负荷，观测电量=Σ(负荷 MW×1h)，单位 MWh；缺失小时不补零。"
    else:
        expressions = "ROUND(AVG(load_mw), 2) AS load_mw, ROUND(AVG(forecast_mw), 2) AS forecast_mw, " + count
        title, ys = "平均负荷趋势", ["load_mw", "forecast_mw"]
        note = "实际与预测均值分别按各自非空小时计算，单位 MW；两者的样本覆盖可能不同。"
    names = {"hour": "小时", "day": "每日", "week": "每周", "total": "整体", "day_type": "工作日与周末"}
    return QueryPlan(
        title=names[task.granularity] + title,
        sql="SELECT " + prefix + expressions + where + suffix,
        chart="line"
        if task.kind == "trend" and period and task.granularity != "day_type"
        else "bar"
        if period
        else "none",
        x=key,
        y=ys if period else [],
        note=note,
    )
