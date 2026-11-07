from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict


class ProposedAction(BaseModel):
    """A proposed action. It is data only; this milestone never executes it."""
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="Name of a tool from the available tool list.")
    arguments: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = Field(
        description="Whether a human must explicitly approve this action."
    )
    reason: str = Field(description="Short explanation for this action.")


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="Short label describing the user's intent.")
    message: str = Field(description="Natural-language response to the user.")
    actions: list[ProposedAction] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    risk_level: Literal["low", "medium", "high"] = "low"
