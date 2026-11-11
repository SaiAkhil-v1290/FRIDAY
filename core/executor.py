from dataclasses import dataclass, field
from ai.schemas import TaskPlan, ProposedAction, validate_action_arguments
from tools.registry import is_registered_tool, TOOL_POLICIES
from security.permissions import approval_manager
from security.audit import audit_logger
from tools.application import launch_application, close_application, check_application_running
from tools.browser import search_browser, get_browser_controller, BrowserActionResult
from tools.files import (
    discover_files,
    FileSearchResult,
    search_file_contents,
    ContentSearchResult,
    open_local_file,
    FileOpenResult,
    reveal_local_file,
    FileRevealResult,
    open_local_folder,
    FolderOpenResult,
)


# Set of tools that have safe deterministic execution implemented in Milestone 3, 4A, 4B, 4B.4, 4B.6, 4D.4 & 5.1
IMPLEMENTED_EXECUTION_TOOLS = {
    "open_application",
    "close_application",
    "check_application_running",
    "browser_search",
    "browser_navigate",
    "browser_go_back",
    "browser_go_forward",
    "browser_refresh",
    "browser_get_page_info",
    "find_files",
    "search_file_contents",
    "open_file",
    "reveal_file",
    "open_folder",
}


@dataclass
class ActionExecutionResult:
    action_name: str
    arguments: dict
    executed: bool
    success: bool
    message: str
    error: str | None = None
    requires_approval: bool = False


@dataclass
class PlanExecutionResult:
    results: list[ActionExecutionResult] = field(default_factory=list)

    @property
    def all_successful(self) -> bool:
        return all(r.success for r in self.results if r.executed)

    @property
    def any_executed(self) -> bool:
        return any(r.executed for r in self.results)


class PlanExecutor:
    """
    Safely executes validated TaskPlans.
    Guarantees:
    - Never executes unregistered tools.
    - Never executes tools without safe execution implementations.
    - Never executes actions requiring approval without explicit confirmation.
    - Catches exceptions so failures never crash the entire application.
    """

    def execute_plan(
        self,
        plan: TaskPlan,
        allow_approved: bool = False,
        stop_on_failure: bool = False,
    ) -> PlanExecutionResult:
        plan_result = PlanExecutionResult()

        if not plan or not plan.actions:
            return plan_result

        for idx, action in enumerate(plan.actions):
            res = self.execute_action(action, allow_approved=allow_approved)
            plan_result.results.append(res)
            # Stop-on-failure policy: If requested and an action failed, halt execution of subsequent actions
            if stop_on_failure and res.executed and not res.success:
                for unexec_action in plan.actions[idx + 1:]:
                    plan_result.results.append(ActionExecutionResult(
                        action_name=unexec_action.tool,
                        arguments=unexec_action.arguments or {},
                        executed=False,
                        success=False,
                        message="Action was not executed because the previous action failed.",
                        error="PREVIOUS_ACTION_FAILED",
                    ))
                break

        return plan_result

    def execute_action(
        self,
        action: ProposedAction,
        allow_approved: bool = False,
        approval_id: str | None = None,
    ) -> ActionExecutionResult:
        tool_name = action.tool
        args = action.arguments or {}

        audit_logger.log(
            "ACTION_EXECUTION_ATTEMPT",
            tool_name,
            "ATTEMPTED",
            arguments=args,
        )

        # 1. Verify registered in TOOL_POLICIES
        if not is_registered_tool(tool_name):
            audit_logger.log(
                "PERMISSION_DENIED",
                tool_name,
                "DENIED",
                reason="Unregistered tool",
            )
            return ActionExecutionResult(
                action_name=tool_name,
                arguments=args,
                executed=False,
                success=False,
                message=f"Action '{tool_name}' is not registered in tool policies.",
                error="UNREGISTERED_TOOL",
            )

        # 2. Check approval requirement
        policy = TOOL_POLICIES.get(tool_name, {})
        must_require_approval = action.requires_approval or bool(policy.get("requires_approval", False))
        target_approval_id = approval_id or getattr(action, "approval_id", None)

        if must_require_approval:
            if target_approval_id:
                # Structured approval token provided - validate and atomically consume
                is_valid, err_msg = approval_manager.validate_and_consume(
                    target_approval_id, tool_name, args
                )
                if not is_valid:
                    audit_logger.log(
                        "APPROVAL_VALIDATION_FAILED",
                        tool_name,
                        "DENIED",
                        approval_id=target_approval_id,
                        error=err_msg,
                    )
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=False,
                        success=False,
                        message=f"Approval validation failed for '{tool_name}': {err_msg}",
                        error="APPROVAL_INVALID",
                        requires_approval=True,
                    )
            else:
                # Security fix: the legacy allow_approved override is intentionally ignored.
                # Approval-gated actions require an explicit ApprovalRequest ID, not a global bypass flag.
                audit_logger.log(
                    "APPROVAL_HELD",
                    tool_name,
                    "HELD",
                    reason="Approval required",
                )
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=False,
                    success=False,
                    message=f"Action '{tool_name}' requires human approval before execution.",
                    error="APPROVAL_REQUIRED",
                    requires_approval=True,
                )

        # 3. Check if execution is implemented for this tool
        if tool_name not in IMPLEMENTED_EXECUTION_TOOLS:
            audit_logger.log(
                "EXECUTION_NOT_IMPLEMENTED",
                tool_name,
                "SKIPPED",
                reason="Tool execution not implemented",
            )
            return ActionExecutionResult(
                action_name=tool_name,
                arguments=args,
                executed=False,
                success=False,
                message=f"Execution for '{tool_name}' is not implemented yet.",
                error="EXECUTION_NOT_IMPLEMENTED",
                requires_approval=must_require_approval,
            )

        # 4. Deterministic argument schema validation
        is_args_valid, args_err = validate_action_arguments(tool_name, args)
        if not is_args_valid:
            audit_logger.log(
                "ARGUMENTS_VALIDATION_FAILED",
                tool_name,
                "REJECTED",
                error=args_err,
            )
            if target_approval_id:
                approval_manager.mark_failed(target_approval_id, f"Invalid arguments: {args_err}")
            return ActionExecutionResult(
                action_name=tool_name,
                arguments=args,
                executed=False,
                success=False,
                message=f"Invalid arguments for action '{tool_name}': {args_err}",
                error="INVALID_ARGUMENTS",
                requires_approval=must_require_approval,
            )

        # 5. Dispatch to tool implementation
        try:
            res = self._dispatch_action(tool_name, args)
        except Exception as exc:
            res = ActionExecutionResult(
                action_name=tool_name,
                arguments=args,
                executed=True,
                success=False,
                message=f"Unexpected error executing '{tool_name}': {exc}",
                error=str(exc),
            )

        if target_approval_id:
            if res.success:
                approval_manager.mark_completed(target_approval_id)
            else:
                approval_manager.mark_failed(target_approval_id, res.message)

        audit_logger.log(
            "ACTION_EXECUTED",
            tool_name,
            "SUCCESS" if res.success else "FAILURE",
            message=res.message,
            error=res.error,
        )
        return res

    def _dispatch_action(self, tool_name: str, args: dict) -> ActionExecutionResult:
            if tool_name == "open_application":
                app_name = args.get("application", "")
                if not app_name:
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=True,
                        success=False,
                        message="Missing 'application' argument in open_application action.",
                        error="MISSING_ARGUMENT",
                    )
                launch_res = launch_application(app_name)
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=launch_res.success,
                    message=launch_res.message,
                    error=launch_res.error,
                )

            elif tool_name == "close_application":
                app_name = args.get("application", "")
                if not app_name:
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=True,
                        success=False,
                        message="Missing 'application' argument in close_application action.",
                        error="MISSING_ARGUMENT",
                    )
                close_res = close_application(app_name)
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=close_res.success,
                    message=close_res.message,
                    error=close_res.error,
                )

            elif tool_name == "check_application_running":
                app_name = args.get("application", "")
                if not app_name:
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=True,
                        success=False,
                        message="Missing 'application' argument in check_application_running action.",
                        error="MISSING_ARGUMENT",
                    )
                status_res = check_application_running(app_name)
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=status_res.success,
                    message=status_res.message,
                    error=status_res.error,
                )

            elif tool_name == "browser_search":
                query_val = args.get("query", "")
                if not query_val or not str(query_val).strip():
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=True,
                        success=False,
                        message="Missing or empty 'query' argument in browser_search action.",
                        error="MISSING_ARGUMENT",
                    )
                search_res = search_browser(str(query_val))
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=search_res.success,
                    message=search_res.message,
                    error=search_res.error,
                )

            elif tool_name == "browser_navigate":
                url_val = args.get("url") or args.get("target") or args.get("website") or ""
                if not url_val or not str(url_val).strip():
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=True,
                        success=False,
                        message="Missing or empty 'url' argument in browser_navigate action.",
                        error="MISSING_ARGUMENT",
                    )
                ctrl = get_browser_controller()
                nav_res = ctrl.open_website(str(url_val))
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=nav_res.success,
                    message=nav_res.message,
                    error=nav_res.error,
                )

            elif tool_name == "browser_go_back":
                ctrl = get_browser_controller()
                nav_res = ctrl.go_back()
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=nav_res.success,
                    message=nav_res.message,
                    error=nav_res.error,
                )

            elif tool_name == "browser_go_forward":
                ctrl = get_browser_controller()
                nav_res = ctrl.go_forward()
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=nav_res.success,
                    message=nav_res.message,
                    error=nav_res.error,
                )

            elif tool_name == "browser_refresh":
                ctrl = get_browser_controller()
                nav_res = ctrl.refresh()
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=nav_res.success,
                    message=nav_res.message,
                    error=nav_res.error,
                )

            elif tool_name == "browser_get_page_info":
                info_type = args.get("type", "page_info")
                ctrl = get_browser_controller()
                if info_type == "title":
                    nav_res = ctrl.get_page_title()
                elif info_type == "url":
                    nav_res = ctrl.get_current_url()
                else:
                    nav_res = ctrl.get_current_page_info()

                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=nav_res.success,
                    message=nav_res.message,
                    error=nav_res.error,
                )

            elif tool_name == "find_files":
                query_val = args.get("query")
                extension_val = args.get("extension")
                extensions_val = args.get("extensions")
                folder_val = args.get("folder") or args.get("path") or args.get("location")
                recursive_val = args.get("recursive", True)
                if isinstance(recursive_val, str):
                    recursive_val = recursive_val.lower() not in {"false", "0", "no"}

                sort_by_val = args.get("sort_by")
                sort_order_val = args.get("sort_order")
                time_filter_val = args.get("time_filter")
                size_filter_val = args.get("size_filter")

                file_res = discover_files(
                    query=query_val,
                    extension=extension_val,
                    extensions=extensions_val,
                    location=folder_val,
                    recursive=recursive_val,
                    sort_by=sort_by_val,
                    sort_order=sort_order_val,
                    time_filter=time_filter_val,
                    size_filter=size_filter_val,
                )

                # Format formatted string in message
                if file_res.success:
                    lines = [file_res.message, ""]
                    # If this was directory listing mode and folders/files are separated
                    if not recursive_val:
                        if file_res.directories:
                            lines.append(f"Folders ({len(file_res.directories)}):")
                            for idx, d in enumerate(file_res.directories, start=1):
                                lines.append(f"{idx}. {d.name}")
                            lines.append("")
                        if file_res.files:
                            lines.append(f"Files ({len(file_res.files)}):")
                            for idx, f in enumerate(file_res.files, start=1):
                                lines.append(f"{idx}. {f.name}")
                        if not file_res.directories and not file_res.files:
                            lines.append("No files or folders found.")
                    else:
                        # Recursive search mode: show filenames and paths, plus metadata if sorted or filtered
                        has_meta_focus = bool(sort_by_val or time_filter_val or size_filter_val)
                        if file_res.files:
                            for idx, f in enumerate(file_res.files, start=1):
                                lines.append(f"{idx}. {f.name}")
                                lines.append(f"   Path: {f.path}")
                                if has_meta_focus:
                                    if f.size is not None:
                                        lines.append(f"   Size: {f.human_size}")
                                    if f.formatted_modified_time is not None:
                                        lines.append(f"   Modified: {f.formatted_modified_time}")
                                if idx < len(file_res.files):
                                    lines.append("")
                        else:
                            lines.append("No matching files found.")
                    full_msg = "\n".join(lines).strip()
                else:
                    full_msg = file_res.message

                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=file_res.success,
                    message=full_msg,
                    error=file_res.error,
                )

            elif tool_name == "search_file_contents":
                query_val = args.get("query", "")
                folder_val = args.get("folder") or args.get("path") or args.get("location")
                extensions_val = args.get("extensions")

                content_res = search_file_contents(
                    query=str(query_val),
                    location=folder_val,
                    extensions=extensions_val,
                )

                if content_res.success:
                    lines = [content_res.message, ""]
                    if content_res.matches:
                        for idx, m in enumerate(content_res.matches, start=1):
                            lines.append(f"{idx}. {m.name}")
                            lines.append(f"   {m.path}")
                            loc_str = m.location if m.location else (f"Line {m.line_number}" if m.line_number else "")
                            if loc_str:
                                lines.append(f"   {loc_str}:")
                            lines.append(f"   ...{m.snippet}...")
                            if idx < len(content_res.matches):
                                lines.append("")
                    full_msg = "\n".join(lines).strip()
                else:
                    full_msg = content_res.message

                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=content_res.success,
                    message=full_msg,
                    error=content_res.error,
                )

            elif tool_name == "open_file":
                file_path_val = args.get("path") or args.get("file_path") or args.get("file")
                if not file_path_val or not str(file_path_val).strip():
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=True,
                        success=False,
                        message="Missing or empty 'path' argument in open_file action.",
                        error="MISSING_ARGUMENT",
                    )
                open_res = open_local_file(str(file_path_val))
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=open_res.success,
                    message=open_res.message,
                    error=open_res.error,
                )

            elif tool_name == "reveal_file":
                file_path_val = args.get("path") or args.get("file_path") or args.get("file")
                if not file_path_val or not str(file_path_val).strip():
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=True,
                        success=False,
                        message="Missing or empty 'path' argument in reveal_file action.",
                        error="MISSING_ARGUMENT",
                    )
                reveal_res = reveal_local_file(str(file_path_val))
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=reveal_res.success,
                    message=reveal_res.message,
                    error=reveal_res.error,
                )

            elif tool_name == "open_folder":
                folder_val = args.get("folder") or args.get("path") or args.get("location")
                if not folder_val or not str(folder_val).strip():
                    return ActionExecutionResult(
                        action_name=tool_name,
                        arguments=args,
                        executed=True,
                        success=False,
                        message="Missing or empty 'folder' argument in open_folder action.",
                        error="MISSING_ARGUMENT",
                    )
                folder_res = open_local_folder(str(folder_val))
                return ActionExecutionResult(
                    action_name=tool_name,
                    arguments=args,
                    executed=True,
                    success=folder_res.success,
                    message=folder_res.message,
                    error=folder_res.error,
                )

            # Unhandled fallback (defensive)
            return ActionExecutionResult(
                action_name=tool_name,
                arguments=args,
                executed=False,
                success=False,
                message=f"Execution dispatcher for '{tool_name}' not configured.",
                error="NO_DISPATCHER",
            )
