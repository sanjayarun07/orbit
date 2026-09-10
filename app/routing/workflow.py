"""Pure workflow transitions. Persistence invalidates returned plan identifiers."""

from enum import Enum
from pydantic import BaseModel, Field


class WorkflowStatus(str, Enum):
    COLLECTING = "collecting_details"
    APPROVAL = "pending_approval"
    SUPERSEDED = "superseded"


class WorkflowState(BaseModel):
    intent: str = "trade"
    status: WorkflowStatus = WorkflowStatus.COLLECTING
    plan_id: str | None = None
    fields: dict = Field(default_factory=dict)

    @classmethod
    def from_context(cls, value: dict | None):
        if not value:
            return None
        return cls(intent=value.get("intent", "trade"), status=value.get("status", "collecting_details"),
                   plan_id=value.get("plan_id"), fields={k: v for k, v in value.items() if k not in {"intent", "status", "plan_id"}})

    def as_context(self):
        return {**self.fields, "intent": self.intent, "status": self.status.value, "plan_id": self.plan_id}


class WorkflowEvent(str, Enum):
    CANCEL = "cancel"
    REPLACE = "replace"
    MODIFY = "modify"
    QUOTE_READY = "quote_ready"
    TOPIC_CHANGE = "topic_change"
    KEEP = "keep"


def apply_event(state: WorkflowState | None, event: WorkflowEvent, replacement: WorkflowState | None = None):
    """Return next state and any pending plan that must be invalidated."""
    if event == WorkflowEvent.KEEP:
        return state, None
    superseded = state.plan_id if state and state.status == WorkflowStatus.APPROVAL else None
    if event in {WorkflowEvent.CANCEL, WorkflowEvent.TOPIC_CHANGE}:
        return None, superseded
    if replacement is None:
        raise ValueError("A replacement workflow is required")
    if event == WorkflowEvent.MODIFY and state:
        replacement = replacement.model_copy(update={"fields": {**state.fields, **{k:v for k,v in replacement.fields.items() if v is not None}}, "plan_id": None})
    if replacement.plan_id == superseded:
        superseded = None
    return replacement, superseded
