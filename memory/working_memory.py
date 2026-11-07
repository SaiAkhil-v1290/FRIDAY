from collections import deque
from dataclasses import dataclass
from config import settings
from ai.schemas import TaskPlan


@dataclass
class ConversationTurn:
    role: str
    content: str
    category: str = "conversation"
    plan: TaskPlan | None = None


class WorkingMemory:
    """In-process conversation context. Discarded when JARVIS exits."""

    def __init__(self) -> None:
        self._turns: deque[ConversationTurn] = deque(
            maxlen=settings.max_history_messages
        )
        self._last_valid_task: tuple[str, TaskPlan] | None = None
        self._last_file_results: list[dict[str, str]] = []
        self._last_file_total_count: int = 0
        self._active_selection_context: dict[str, object] | None = None
        self._last_opened_file: dict[str, str] | None = None
        self._last_application: dict[str, str] | None = None

    @property
    def messages(self) -> list[dict[str, str]]:
        return [{"role": turn.role, "content": turn.content} for turn in self._turns]

    def set_active_file_selection(
        self,
        candidates: list[dict[str, str]],
        source: str = "",
        is_clarification: bool = False,
        total_count: int | None = None,
    ) -> None:
        """
        Set or update the active file-selection context.
        Whenever a clarification list is presented or a search result is remembered,
        this active context guarantees:
            DISPLAYED_FILE_SELECTION_LIST == ACTIVE_FILE_SELECTION_CONTEXT
        """
        bounded = []
        for item in candidates[:100]:
            if isinstance(item, dict) and "path" in item:
                bounded.append({
                    "name": str(item.get("name", "")),
                    "path": str(item.get("path", "")),
                    "extension": str(item.get("extension", "")).lower() if item.get("extension") else "",
                })
        self._active_selection_context = {
            "candidates": bounded,
            "total_count": total_count if total_count is not None else len(candidates),
            "source": source,
            "is_clarification": is_clarification,
            "current_index": None,
        }
        # Also maintain compatibility with _last_file_results
        self._last_file_results = bounded
        self._last_file_total_count = total_count if total_count is not None else len(candidates)

    def get_active_file_selection(self) -> dict[str, object] | None:
        """Return the current active file-selection context, or None."""
        return self._active_selection_context

    def set_current_file_selection_index(self, index: int | None) -> None:
        """
        Update the 1-based current selection index within the active file selection context.
        Ensures index is valid (1 <= index <= len(candidates)). If invalid or no active context,
        does nothing or sets to None.
        """
        if self._active_selection_context is None:
            return
        if index is None:
            self._active_selection_context["current_index"] = None
            return
        candidates = self._active_selection_context.get("candidates", [])
        if 1 <= index <= len(candidates):
            self._active_selection_context["current_index"] = index

    def get_current_file_selection_index(self) -> int | None:
        """Return the current 1-based selection index in the active context, or None."""
        if self._active_selection_context:
            return self._active_selection_context.get("current_index")
        return None

    def remember_file_results(self, results: list[dict[str, str]], total_count: int | None = None, source: str = "") -> None:
        """Store the most recent list of file results in a structured, bounded way."""
        if results is not None:
            self.set_active_file_selection(
                candidates=results,
                source=source,
                is_clarification=False,
                total_count=total_count,
            )

    def get_last_file_results(self) -> list[dict[str, str]]:
        """Return the most recent list of file results from active context or last results."""
        if self._active_selection_context and self._active_selection_context.get("candidates"):
            return list(self._active_selection_context["candidates"])
        return list(self._last_file_results)

    def get_last_file_total_count(self) -> int:
        """Return the total match count from the active context or most recent search."""
        if self._active_selection_context:
            tc = self._active_selection_context.get("total_count", 0)
            cands = self._active_selection_context.get("candidates", [])
            return max(int(tc) if tc else 0, len(cands))
        return max(self._last_file_total_count, len(self._last_file_results))

    def add(
        self,
        role: str,
        content: str,
        category: str = "conversation",
        plan: TaskPlan | None = None,
    ) -> None:
        self._turns.append(
            ConversationTurn(
                role=role,
                content=content,
                category=category,
                plan=plan,
            )
        )

    def set_last_task(self, user_request: str, plan: TaskPlan) -> None:
        """Explicitly store the newest validated task with non-empty actions."""
        if plan and plan.actions:
            self._last_valid_task = (user_request, plan)

    def get_last_task(self) -> tuple[str, TaskPlan] | None:
        """
        Return the most recent valid task.
        Uses _last_valid_task if set, or scans turns backwards.
        """
        if self._last_valid_task and self._last_valid_task[1].actions:
            return self._last_valid_task

        for i in range(len(self._turns) - 1, -1, -1):
            turn = self._turns[i]
            if turn.category in {"task", "dangerous_task", "continuation"} and turn.plan and turn.plan.actions:
                user_req = turn.content
                if turn.role == "assistant" and i > 0 and self._turns[i - 1].role == "user":
                    user_req = self._turns[i - 1].content
                return user_req, turn.plan
        return None

    def get_last_file_search(self) -> dict[str, object] | None:
        """
        Return structured context of the most recent local file search task.
        Extracts only safe parameters: tool ('find_files' or 'search_file_contents'),
        folder, query, and extensions.
        Never returns destructive or arbitrary commands.
        """
        for i in range(len(self._turns) - 1, -1, -1):
            turn = self._turns[i]
            if turn.plan and turn.plan.actions:
                for action in turn.plan.actions:
                    if action.tool in {"find_files", "search_file_contents"}:
                        return {
                            "tool": action.tool,
                            "folder": action.arguments.get("folder"),
                            "query": action.arguments.get("query"),
                            "extensions": action.arguments.get("extensions"),
                            "extension": action.arguments.get("extension"),
                        }
        return None

    def set_last_opened_file(self, file_info: dict[str, str]) -> None:
        """Remember the most recently opened file for safe follow-up reference ('open it again')."""
        if file_info and isinstance(file_info, dict) and "path" in file_info:
            self._last_opened_file = {
                "name": str(file_info.get("name", "")),
                "path": str(file_info.get("path", "")),
                "extension": str(file_info.get("extension", "")).lower() if file_info.get("extension") else "",
            }

    def get_last_opened_file(self) -> dict[str, str] | None:
        """Return the most recently opened file info, or None."""
        return dict(self._last_opened_file) if self._last_opened_file else None

    def set_last_application(self, app_info: dict[str, str]) -> None:
        """
        Remember the most recently successfully interacted-with approved application.
        Must originate from APPLICATION_ALLOWLIST (canonical key and display_name).
        Never stores arbitrary process names or unapproved applications.
        """
        if app_info and isinstance(app_info, dict) and "key" in app_info and "display_name" in app_info:
            self._last_application = {
                "key": str(app_info["key"]),
                "display_name": str(app_info["display_name"]),
            }

    def get_last_application(self) -> dict[str, str] | None:
        """Return the most recently interacted approved application, or None."""
        return dict(self._last_application) if self._last_application else None

    def clear_last_application(self) -> None:
        """Clear the remembered application context."""
        self._last_application = None

    def clear(self) -> None:
        self._turns.clear()
        self._last_valid_task = None
        self._last_file_results = []
        self._last_file_total_count = 0
        self._active_selection_context = None
        self._last_opened_file = None
        self._last_application = None
