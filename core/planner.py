import re
from ai.schemas import TaskPlan, ProposedAction
from ai.intent import extract_explicit_search_query, extract_explicit_file_search_params
from tools.registry import TOOL_POLICIES, is_registered_tool

# Concepts that must strictly require human approval
DANGEROUS_ACTION_KEYWORDS = [
    "delete", "remove", "erase", "wipe", "format", "destroy", "send", "submit", "purchase", "buy", "order", "pay", "transfer"
]

SUSPICIOUS_PATH_PATTERNS = [
    r"yourusername",
    r"<username>",
    r"username",
    r"user_name",
    r"users\\[^\\]+\\",  # e.g., C:\Users\something\
    r"/home/[^/]+/",     # e.g., /home/something/
]

LOGICAL_FOLDERS = ["downloads", "documents", "desktop", "pictures", "music", "videos"]


def sanitize_folder_path(raw_path: str) -> str:
    """
    Ensure no invented Windows paths or fake usernames are used.
    Normalize logical folder names (Downloads, Documents, Desktop, etc.).
    """
    path_lower = raw_path.lower()
    for folder in LOGICAL_FOLDERS:
        if folder in path_lower:
            return folder.capitalize()

    for pattern in SUSPICIOUS_PATH_PATTERNS:
        if re.search(pattern, path_lower):
            return "Downloads"

    return raw_path


def validate_plan(plan: TaskPlan, current_request: str | None = None) -> TaskPlan:
    """
    Apply defensive policy checks, safety rules, and consistency filters.
    Never crashes on invalid model output.
    """
    # 1. Clarification handling
    if plan.needs_clarification:
        plan.actions = []
        return plan

    req_lower = (current_request or "").lower()

    # Rejection of instruction injection attempts
    if any(k in req_lower for k in ["ignore previous instructions", "ignore all previous instructions", "disregard", "override your rules"]):
        plan.actions = []
        plan.message = "I can't follow instructions that attempt to override JARVIS's safety rules. Please give me the task normally."
        return plan

    # Legitimate dangerous task check: requires explicit file operations or emails/money
    # NOTE: Web searches (such as searching how to delete files) are NOT local dangerous tasks.
    is_search = extract_explicit_search_query(current_request or "") is not None
    is_legitimate_deletion = (not is_search) and any(k in req_lower for k in ["delete all files", "delete files", "delete my downloads", "remove all files", "erase files", "delete all", "delete the files", "delete these files", "delete this file"])
    is_legitimate_email = (not is_search) and ("send" in req_lower and "email" in req_lower)
    is_dangerous_request = is_legitimate_deletion or is_legitimate_email or plan.intent == "dangerous_task"

    valid_actions: list[ProposedAction] = []

    for action in plan.actions:
        # 2. Reject unregistered / hallucinated tools safely
        if not is_registered_tool(action.tool):
            continue

        # 3. Defensive path normalization
        if action.tool != "open_folder" and "folder" in action.arguments and isinstance(action.arguments["folder"], str):
            action.arguments["folder"] = sanitize_folder_path(action.arguments["folder"])
        if action.tool not in {"open_file", "open_folder"} and "path" in action.arguments and isinstance(action.arguments["path"], str):
            action.arguments["path"] = sanitize_folder_path(action.arguments["path"])

        # 4. Strict semantic relevance checks
        # delete_files must NOT be created unless the user explicitly asked to delete/remove files
        if action.tool == "delete_files":
            if not any(w in req_lower for w in ["delete", "remove", "erase", "wipe", "clean", "trash"]):
                continue

        # find_files must be related to files/folders/documents/resume or an explicit file discovery request
        if action.tool == "find_files":
            is_explicit_file_req = extract_explicit_file_search_params(current_request or "") is not None
            file_keywords = ["file", "files", "folder", "folders", "resume", "cv", "doc", "document", "directory", "disk"]
            has_keyword = any(kw in req_lower for kw in file_keywords)
            if not is_explicit_file_req and not has_keyword:
                continue

        # search_file_contents must have a non-empty query and be related to searching content
        if action.tool == "search_file_contents":
            query_str = str(action.arguments.get("query", "")).strip()
            if not query_str:
                continue

        # open_application: ensure non-empty canonical application argument
        if action.tool == "open_application":
            app_arg = str(action.arguments.get("application") or action.arguments.get("app") or action.arguments.get("name") or "").strip()
            if not app_arg and current_request:
                from ai.intent import extract_explicit_open_app_target
                app_arg = extract_explicit_open_app_target(current_request) or ""
            if not app_arg:
                continue
            # Also resolve canonical name through allowlist if possible
            from tools.application import resolve_application
            resolved = resolve_application(app_arg)
            if resolved:
                app_arg = resolved[1].get("display_name", resolved[0])
            action.arguments["application"] = app_arg

        # close_application: ensure non-empty canonical application argument
        if action.tool == "close_application":
            app_arg = str(action.arguments.get("application") or action.arguments.get("app") or action.arguments.get("name") or "").strip()
            if not app_arg and current_request:
                from ai.intent import extract_explicit_close_app_target
                app_arg = extract_explicit_close_app_target(current_request) or ""
            if not app_arg:
                continue
            from tools.application import resolve_application
            resolved = resolve_application(app_arg)
            if resolved:
                app_arg = resolved[1].get("display_name", resolved[0])
            action.arguments["application"] = app_arg

        # check_application_running: ensure non-empty canonical application argument
        if action.tool == "check_application_running":
            app_arg = str(action.arguments.get("application") or action.arguments.get("app") or action.arguments.get("name") or "").strip()
            if not app_arg and current_request:
                from ai.intent import extract_explicit_app_status_target
                app_arg = extract_explicit_app_status_target(current_request) or ""
            if not app_arg:
                continue
            from tools.application import resolve_application
            resolved = resolve_application(app_arg)
            if resolved:
                app_arg = resolved[1].get("display_name", resolved[0])
            action.arguments["application"] = app_arg

        # read_screen must be related to screen/display
        if action.tool == "read_screen":
            screen_keywords = ["screen", "display", "look at", "see", "error on", "window"]
            if not any(kw in req_lower for kw in screen_keywords):
                continue

        # browser_search: ensure query is valid and user asked for search / lookup
        if action.tool == "browser_search":
            query_str = str(action.arguments.get("query", "")).strip()
            if not query_str:
                continue
            # Browser automation/interaction phrases that are NOT searches
            if any(p in req_lower for p in ["click the", "click on", "enter my password", "download this file", "fill out this form"]):
                continue

        # browser_navigate: ensure valid URL/website argument
        if action.tool == "browser_navigate":
            url_str = str(action.arguments.get("url") or action.arguments.get("target") or "").strip()
            if not url_str and current_request:
                from ai.intent import extract_browser_navigation_target
                url_str = extract_browser_navigation_target(current_request) or ""
            if not url_str:
                continue
            from tools.browser import validate_and_normalize_url
            norm_url, err = validate_and_normalize_url(url_str)
            if err or not norm_url:
                continue
            action.arguments["url"] = norm_url

        # open_file: ensure non-empty path resolving inside an approved safe root
        if action.tool == "open_file":
            path_val = str(action.arguments.get("path") or action.arguments.get("file_path") or "").strip()
            if not path_val:
                continue
            from tools.files import resolve_safe_root
            resolved_p, err = resolve_safe_root(path_val)
            if err or not resolved_p:
                continue
            action.arguments["path"] = str(resolved_p)

        # open_folder: ensure folder is one of the allowed safe logical folders
        if action.tool == "open_folder":
            f_arg = str(action.arguments.get("folder") or action.arguments.get("path") or "").strip().lower()
            if not f_arg and current_request:
                from ai.intent import extract_explicit_open_folder_target
                f_arg = (extract_explicit_open_folder_target(current_request) or "").lower()
            m_f = re.search(r"\b(desktop|documents|downloads)\b", f_arg)
            if not m_f:
                continue
            action.arguments["folder"] = m_f.group(1).lower()

        # 5. Mandatory approval enforcement
        policy = TOOL_POLICIES[action.tool]
        if policy["requires_approval"] or is_dangerous_request or action.tool in {"delete_files", "send_email", "draft_email"}:
            action.requires_approval = True
        else:
            action.requires_approval = action.requires_approval or policy["requires_approval"]

        valid_actions.append(action)

    plan.actions = valid_actions

    # 6. Fallback synthesis ONLY if user explicitly asked for legitimate file deletion or email sending
    if is_dangerous_request and not plan.actions:
        if is_legitimate_deletion:
            folder_name = "Downloads"
            for f in LOGICAL_FOLDERS:
                if f in req_lower:
                    folder_name = f.capitalize()
                    break
            plan.actions = [
                ProposedAction(
                    tool="delete_files",
                    arguments={"folder": folder_name, "pattern": "all" if "all" in req_lower else "*"},
                    requires_approval=True,
                    reason="User requested file deletion; mandatory approval required.",
                )
            ]
            plan.risk_level = "high"
        elif is_legitimate_email:
            plan.actions = [
                ProposedAction(
                    tool="send_email",
                    arguments={"recipient": "recipient", "subject": "Subject", "body": "Body content"},
                    requires_approval=True,
                    reason="User requested sending an email; mandatory approval required.",
                )
            ]
            plan.risk_level = "high"

    # 7. Check if any action requires approval or is dangerous
    if any(a.requires_approval for a in plan.actions) or is_dangerous_request:
        plan.risk_level = "high"

        # 8. Deterministic safe messaging
        if is_legitimate_deletion:
            plan.message = (
                "I can prepare a plan to delete the requested files, but nothing has been deleted.\n"
                "This action requires your explicit approval before execution."
            )
        elif is_legitimate_email:
            plan.message = (
                "I can prepare the email, but it has not been sent.\n"
                "Sending it requires your explicit approval."
            )
        else:
            plan.message = (
                "This action is planned but has not been executed.\n"
                "It requires your explicit approval before execution."
            )

    return plan
