import re
from datetime import date, datetime, timedelta

from .errors import AgentError
from .models import AskRequest, Scope


class ScopeError(AgentError):
    default_code = "invalid_scope"


def resolve_scope(request: AskRequest, context: dict) -> Scope:
    question = request.question
    regions = {r["region"]: r for r in context["regions"]}
    if re.search(r"所有区域|各区域|全部区域|all\s+(?:regions|areas)", question, re.I):
        raise ScopeError("首版每次分析一个区域，请选择一个区域后分别查询。")
    mentions = [
        r
        for r in regions
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(r) + r"(?![A-Za-z0-9_])", question, re.I)
    ]
    if len(mentions) > 1:
        raise ScopeError("首版每次分析一个区域，请选择一个区域后分别查询。")
    region = (
        mentions[0]
        if mentions
        else request.region or ("PJM_RTO" if "PJM_RTO" in regions else next(iter(regions)))
    )
    if region not in regions:
        raise ScopeError("未知区域。可选区域：" + ", ".join(regions))
    if mentions and request.region and mentions[0] != request.region:
        raise ScopeError("问题中的区域与筛选器不同，请统一区域。")
    coverage = regions[region]
    first = date.fromisoformat(coverage["start"][:10])
    last = date.fromisoformat(coverage["end"][:10])
    note = "时间均为 UTC；未指定日期时默认使用数据中最后 7 个日历日。"
    try:
        dates = re.findall(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", question)
        relative = re.search(r"(?:最近|过去|last|past)\s*(\d+)\s*(?:天|days?)", question, re.I)
        has_relative = relative or re.search(
            r"上周|最近一周|过去一周|last week|yesterday|昨天", question, re.I
        )
        if dates and has_relative:
            raise ScopeError("请使用绝对日期或相对日期中的一种，避免歧义。")
        if request.start_date or request.end_date:
            if not (request.start_date and request.end_date):
                raise ScopeError("开始和结束日期必须同时提供。")
            start, end = date.fromisoformat(request.start_date), date.fromisoformat(request.end_date)
            if dates and (dates[0] != request.start_date or dates[-1] != request.end_date):
                raise ScopeError("问题中的日期与筛选器不同，请统一日期范围。")
            if has_relative:
                raise ScopeError("相对日期与手动日期筛选不能同时使用，请清空日期筛选器。")
            note = "使用手动日期范围，首尾日均包含；时间均为 UTC。"
        elif dates:
            if len(dates) > 2:
                raise ScopeError("每次最多提供两个 ISO 日期。")
            start, end = date.fromisoformat(dates[0]), date.fromisoformat(dates[-1])
            note = "使用问题中的日期范围，首尾日均包含；时间均为 UTC。"
        elif has_relative:
            days = int(relative[1]) if relative else 7
            if re.search(r"上周|last week", question, re.I):
                end = last - timedelta(days=last.weekday() + 1)
                note = f"以最新数据日 {last} 为基准，使用上一个完整 UTC 日历周（周一至周日）。"
            elif re.search(r"yesterday|昨天", question, re.I):
                end, days = last - timedelta(days=1), 1
                note = f"以最新数据日 {last} 为基准，查询前一天（UTC）。"
            else:
                end = last
                note = f"相对时间以该区域最新数据日 {last} 为基准，使用最近 {days} 个日历日，UTC。"
            if not 1 <= days <= 366:
                raise ScopeError("相对日期范围应在 1–366 天内。")
            start = end - timedelta(days=days - 1)
        else:
            start, end = max(first, last - timedelta(days=6)), last
        if start > end:
            raise ScopeError("开始日期不能晚于结束日期。")
        if (end - start).days >= 366:
            raise ScopeError("每次最多查询 366 天。")
        if start < first or end > last:
            raise ScopeError(f"超出数据范围；{region} 可用日期为 {first} 至 {last}（UTC）。")
        # Reject common unparsed date expressions instead of silently selecting the last week.
        if (
            not dates
            and not has_relative
            and not request.start_date
            and re.search(
                r"\d{4}年|\d{1,2}月|\d{1,2}/\d{1,2}|\d{4}-\d{1,2}-\d{1,2}|"
                r"today|今天|上个月|last month|this week|本周|(?:最近|过去).{0,4}(?:天|周|月|年)|"
                r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b",
                question,
                re.I,
            )
        ):
            raise ScopeError("请用 YYYY-MM-DD 表达日期，或使用“最近 7 天”。")
    except ValueError as exc:
        if isinstance(exc, ScopeError):
            raise
        raise ScopeError("日期格式应为 YYYY-MM-DD，且日期必须存在。") from exc
    return Scope(
        start=datetime.combine(start, datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        end=datetime.combine(end + timedelta(days=1), datetime.min.time()).strftime("%Y-%m-%dT%H:%M:%SZ"),
        region=region,
        note=note,
    )
