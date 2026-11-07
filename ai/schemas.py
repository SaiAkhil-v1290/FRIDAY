from typing import Any, Literal
from pydantic import BaseModel, Field, ConfigDict, field_validator
from tools.registry import is_registered_tool


class ProposedAction(BaseModel):
    """
    A proposed action resulting from intent planning.
    It is strictly data and validation logic; execution is handled deterministically by PlanExecutor.
    """
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(description="Name of a registered tool from the registry.")
    arguments: dict[str, Any] = Field(default_factory=dict, description="Dictionary of tool arguments.")
    requires_approval: bool = Field(description="Whether explicit user confirmation is required.")
    reason: str = Field(description="Short explanation for this proposed action.")
    approval_id: str | None = Field(default=None, description="Optional associated ApprovalRequest ID.")

    @field_validator("tool")
    @classmethod
    def validate_tool_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Tool name cannot be empty.")
        return cleaned

    @field_validator("arguments")
    @classmethod
    def validate_arguments_dict(cls, v: Any) -> dict[str, Any]:
        if not isinstance(v, dict):
            raise ValueError("Arguments must be a dictionary.")
        # Ensure keys are non-empty strings
        for k in v.keys():
            if not isinstance(k, str) or not k.strip():
                raise ValueError(f"Argument key '{k}' must be a non-empty string.")
        return v


class TaskPlan(BaseModel):
    """
    A structured plan representing user intent and proposed sequence of actions.
    """
    model_config = ConfigDict(extra="forbid")

    intent: str = Field(description="Short label describing the user's intent.")
    message: str = Field(description="Natural-language response to the user.")
    actions: list[ProposedAction] = Field(default_factory=list)
    needs_clarification: bool = False
    clarification_question: str | None = None
    risk_level: Literal["low", "medium", "high", "critical"] = "low"

    @field_validator("intent")
    @classmethod
    def validate_intent(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("Intent cannot be empty.")
        return cleaned


# Deterministic argument validation schemas for individual tools

class OpenApplicationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application: str = Field(min_length=1)


class CloseApplicationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application: str = Field(min_length=1)


class CheckApplicationArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    application: str = Field(min_length=1)


class BrowserSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)


class BrowserNavigateArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1)


class BrowserPageInfoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["page_info", "title", "url"] = "page_info"


class FileSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = None
    extension: str | None = None
    extensions: list[str] | None = None
    folder: str | None = None
    path: str | None = None
    location: str | None = None
    recursive: bool = True
    sort_by: str | None = None
    sort_order: str | None = None
    time_filter: str | None = None
    size_filter: dict[str, Any] | None = None


class SearchFileContentsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1)
    folder: str | None = None
    path: str | None = None
    location: str | None = None
    extensions: list[str] | None = None


class FilePathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str | None = None
    file_path: str | None = None
    file: str | None = None


class FolderPathArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str | None = None
    path: str | None = None
    location: str | None = None


class DeleteFilesArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str = Field(min_length=1)
    pattern: str = Field(default="*")


class EmailArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: str = Field(min_length=1)
    subject: str = Field(default="")
    body: str = Field(default="")


TOOL_ARGUMENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "open_application": OpenApplicationArgs,
    "close_application": CloseApplicationArgs,
    "check_application_running": CheckApplicationArgs,
    "browser_search": BrowserSearchArgs,
    "browser_navigate": BrowserNavigateArgs,
    "browser_get_page_info": BrowserPageInfoArgs,
    "find_files": FileSearchArgs,
    "search_file_contents": SearchFileContentsArgs,
    "open_file": FilePathArgs,
    "reveal_file": FilePathArgs,
    "open_folder": FolderPathArgs,
    "delete_files": DeleteFilesArgs,
    "send_email": EmailArgs,
    "draft_email": EmailArgs,
}


def validate_action_arguments(tool: str, arguments: dict[str, Any]) -> tuple[bool, str | None]:
    """
    Deterministically validate tool arguments against strict schema.
    Returns (True, None) if valid, or (False, error_description).
    """
    if not is_registered_tool(tool):
        return False, f"Tool '{tool}' is not registered."

    schema_cls = TOOL_ARGUMENT_SCHEMAS.get(tool)
    if not schema_cls:
        # Tool without arguments (e.g. browser_go_back, browser_go_forward, browser_refresh, read_screen)
        if arguments:
            # Check if non-empty arguments were unexpectedly provided
            return True, None
        return True, None

    try:
        schema_cls.model_validate(arguments or {})
        return True, None
    except Exception as exc:
        return False, str(exc)
