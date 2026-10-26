"""Machine-readable failure categories; infrastructure failure is never a refusal."""


class AgentError(ValueError):
    default_code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None):
        super().__init__(message)
        self.code = code or self.default_code


class PlanningError(AgentError):
    default_code = "planning_error"


class SemanticError(AgentError):
    default_code = "semantic_error"


REFUSALS = {"policy_refusal", "data_unavailable", "unsupported_question"}


def failure_outcome(code: str) -> str:
    if code in REFUSALS:
        return "refused"
    if code == "invalid_scope":
        return "rejected"
    return "failed"
