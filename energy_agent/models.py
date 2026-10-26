from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=2, max_length=2000)
    region: str | None = Field(default=None, max_length=80)
    start_date: str | None = None
    end_date: str | None = None


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    sql: str
    chart: Literal["line", "bar", "none"]
    x: str
    y: list[str]
    note: str


class Scope(BaseModel):
    start: str
    end: str
    region: str
    note: str = ""


class AnalysisTask(BaseModel):
    """A bounded domain contract, separate from model-generated SQL and presentation."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["trend", "peak", "forecast", "energy", "anomaly", "count", "custom"]
    granularity: Literal["hour", "day", "week", "total", "day_type"] = "day"
    top_k: int = Field(default=5, ge=1, le=100)
    day_filter: Literal["all", "weekday", "weekend"] = "all"
    unit: str = "MW"
