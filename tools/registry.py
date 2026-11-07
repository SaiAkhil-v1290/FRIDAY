# This registry describes capabilities only. No tool execution is implemented
# in Milestone 1. Policies are enforced by the orchestrator/planner.
TOOL_POLICIES: dict[str, dict[str, bool]] = {
    "open_application": {"requires_approval": False},
    "close_application": {"requires_approval": False},
    "check_application_running": {"requires_approval": False},
    "browser_search": {"requires_approval": False},
    "browser_navigate": {"requires_approval": False},
    "browser_go_back": {"requires_approval": False},
    "browser_go_forward": {"requires_approval": False},
    "browser_refresh": {"requires_approval": False},
    "browser_get_page_info": {"requires_approval": False},
    "read_screen": {"requires_approval": True},
    "find_files": {"requires_approval": False},
    "search_file_contents": {"requires_approval": False},
    "open_file": {"requires_approval": False},
    "reveal_file": {"requires_approval": False},
    "open_folder": {"requires_approval": False},
    "create_document": {"requires_approval": False},
    "draft_email": {"requires_approval": True},
    "send_email": {"requires_approval": True},
    "delete_files": {"requires_approval": True},
}


def is_registered_tool(name: str) -> bool:
    return name in TOOL_POLICIES
