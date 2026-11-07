"""
Central tool registry defining registered capabilities, approval policies,
and risk classifications.
"""

TOOL_POLICIES: dict[str, dict[str, object]] = {
    "open_application": {
        "requires_approval": False,
        "category": "application_management",
        "risk_level": "medium",
        "description": "Launch an allowlisted desktop application.",
    },
    "close_application": {
        "requires_approval": False,
        "category": "application_management",
        "risk_level": "medium",
        "description": "Terminate an allowlisted desktop application process.",
    },
    "check_application_running": {
        "requires_approval": False,
        "category": "system_info",
        "risk_level": "low",
        "description": "Query running status of an allowlisted desktop application.",
    },
    "browser_search": {
        "requires_approval": False,
        "category": "navigation",
        "risk_level": "low",
        "description": "Perform a read-only web search using default browser.",
    },
    "browser_navigate": {
        "requires_approval": False,
        "category": "navigation",
        "risk_level": "low",
        "description": "Navigate default browser to an explicit normalized URL.",
    },
    "browser_go_back": {
        "requires_approval": False,
        "category": "navigation",
        "risk_level": "low",
        "description": "Navigate backward in browser history.",
    },
    "browser_go_forward": {
        "requires_approval": False,
        "category": "navigation",
        "risk_level": "low",
        "description": "Navigate forward in browser history.",
    },
    "browser_refresh": {
        "requires_approval": False,
        "category": "navigation",
        "risk_level": "low",
        "description": "Refresh the active browser tab.",
    },
    "browser_get_page_info": {
        "requires_approval": False,
        "category": "navigation",
        "risk_level": "low",
        "description": "Read title or URL of the active browser tab.",
    },
    "read_screen": {
        "requires_approval": True,
        "category": "local_sensitive",
        "risk_level": "high",
        "description": "Capture or inspect desktop screen content.",
    },
    "find_files": {
        "requires_approval": False,
        "category": "read_only",
        "risk_level": "low",
        "description": "Safely locate files within approved root directories.",
    },
    "search_file_contents": {
        "requires_approval": False,
        "category": "read_only",
        "risk_level": "low",
        "description": "Search literal text inside supported files within approved roots.",
    },
    "open_file": {
        "requires_approval": False,
        "category": "local_sensitive",
        "risk_level": "medium",
        "description": "Open a local file inside approved root directories using OS viewer.",
    },
    "reveal_file": {
        "requires_approval": False,
        "category": "read_only",
        "risk_level": "low",
        "description": "Reveal a file location inside approved root directories in File Explorer.",
    },
    "open_folder": {
        "requires_approval": False,
        "category": "read_only",
        "risk_level": "low",
        "description": "Open an approved user root folder in File Explorer.",
    },
    "create_document": {
        "requires_approval": False,
        "category": "local_sensitive",
        "risk_level": "medium",
        "description": "Create a new document file in an approved directory.",
    },
    "draft_email": {
        "requires_approval": True,
        "category": "communication",
        "risk_level": "high",
        "description": "Prepare an email draft requiring user review.",
    },
    "send_email": {
        "requires_approval": True,
        "category": "communication",
        "risk_level": "high",
        "description": "Send an email externally; strictly requires explicit approval.",
    },
    "delete_files": {
        "requires_approval": True,
        "category": "destructive",
        "risk_level": "critical",
        "description": "Permanently delete files; strictly requires explicit approval.",
    },
}


def is_registered_tool(name: str) -> bool:
    """Check if tool name is explicitly registered in policy dictionary."""
    return name in TOOL_POLICIES


def get_tool_policy(name: str) -> dict[str, object] | None:
    """Retrieve policy specification for a registered tool, or None if unregistered."""
    return TOOL_POLICIES.get(name)
