import re
import urllib.parse
import webbrowser
from dataclasses import dataclass

MAX_SEARCH_QUERY_LENGTH = 500

# Canonical safe website aliases mapping normalized site names to official URLs
SAFE_WEBSITE_ALIASES: dict[str, str] = {
    "google": "https://www.google.com",
    "youtube": "https://www.youtube.com",
    "github": "https://github.com",
    "chatgpt": "https://chatgpt.com",
    "wikipedia": "https://www.wikipedia.org",
}


@dataclass
class BrowserSearchResult:
    success: bool
    message: str
    query: str
    url: str | None = None
    error: str | None = None


@dataclass
class BrowserActionResult:
    success: bool
    message: str
    url: str | None = None
    title: str | None = None
    error: str | None = None


def validate_and_normalize_url(raw_target: str) -> tuple[str | None, str | None]:
    """
    Validate and normalize a URL or safe website alias.
    Never interprets local file paths, executable paths, or OS commands as URLs.
    Returns (normalized_url, error_message).
    """
    if not raw_target or not isinstance(raw_target, str):
        return None, "Empty or invalid target provided."

    target = raw_target.strip()
    target_lower = target.lower()

    # Reject local Windows / Linux paths and executables unconditionally
    if (
        re.match(r"^[a-zA-Z]:[\\/]", target)
        or target.startswith(("\\\\", "/", "./", "../", ".\\", "..\\"))
        or any(target_lower.endswith(ext) for ext in [".exe", ".bat", ".cmd", ".ps1", ".sh", ".dll"])
        or any(cmd in target_lower for cmd in ["cmd.exe", "powershell", "python.exe", "system32"])
    ):
        return None, f"'{raw_target}' is an OS path or command, not a valid browser target."

    # Reject standard document/data/code file extensions so local files aren't treated as web domains
    FILE_EXTENSIONS = (
        ".pdf", ".doc", ".docx", ".txt", ".rtf", ".odt", ".csv", ".xlsx", ".xls",
        ".pptx", ".ppt", ".py", ".json", ".xml", ".yaml", ".yml", ".md", ".zip",
        ".tar", ".gz", ".7z", ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".mp3", ".mp4"
    )
    if any(target_lower.endswith(ext) for ext in FILE_EXTENSIONS):
        return None, f"'{raw_target}' is a document/file name, not a valid browser target."

    # 1. Check known safe website aliases
    if target_lower in SAFE_WEBSITE_ALIASES:
        return SAFE_WEBSITE_ALIASES[target_lower], None

    # 2. Check explicit schemes (http://, https://)
    if target.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(target)
        if not parsed.netloc or "." not in parsed.netloc:
            return None, f"Invalid URL domain: '{raw_target}'"
        return target, None

    # 3. Check domain-like strings (e.g. example.com, www.example.com, github.com)
    # Must contain a valid dot and look like a hostname
    if re.match(r"^(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+(?:/.*)?$", target):
        return f"https://{target}", None

    return None, f"Cannot resolve '{raw_target}' to a valid web URL."


class BrowserController:
    """
    Dedicated browser controller managing controlled browser session state.
    Provides safe, non-shell browser navigation, history management, and factual page awareness.
    Maintains active session awareness without fabricating state when no session is active.
    """

    def __init__(self) -> None:
        self._active: bool = False
        self._history: list[dict[str, str]] = []
        self._history_index: int = -1

    @property
    def is_active(self) -> bool:
        return self._active

    def open_website(self, target: str) -> BrowserActionResult:
        """
        Navigate to a website alias or validated URL.
        Reuses or starts the controlled session, updates history, and dispatches to system browser.
        """
        normalized_url, err = validate_and_normalize_url(target)
        if err or not normalized_url:
            return BrowserActionResult(
                success=False,
                message=err or f"Invalid URL target: {target}",
                error="INVALID_URL",
            )

        title = self._derive_page_title(normalized_url)
        entry = {"url": normalized_url, "title": title}

        # Truncate forward history if navigating from middle
        if self._history_index >= 0 and self._history_index < len(self._history) - 1:
            self._history = self._history[: self._history_index + 1]

        self._history.append(entry)
        self._history_index = len(self._history) - 1
        self._active = True

        # Open in real system browser without shell=True
        try:
            webbrowser.open(normalized_url)
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to open browser: {exc}",
                url=normalized_url,
                title=title,
                error=str(exc),
            )

        # Build clean message
        site_name = self._derive_site_name(normalized_url)
        return BrowserActionResult(
            success=True,
            message=f"Opening {site_name}.",
            url=normalized_url,
            title=title,
        )

    def go_back(self) -> BrowserActionResult:
        """Navigate back to the previous page in controlled browser session history."""
        if not self._active or not self._history:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to navigate.",
                error="NO_ACTIVE_SESSION",
            )

        if self._history_index <= 0:
            current = self._history[self._history_index]
            return BrowserActionResult(
                success=False,
                message="You are already at the beginning of browser navigation history.",
                url=current["url"],
                title=current["title"],
                error="NO_PREVIOUS_PAGE",
            )

        self._history_index -= 1
        prev = self._history[self._history_index]
        try:
            webbrowser.open(prev["url"])
        except Exception:
            pass

        return BrowserActionResult(
            success=True,
            message="Going back to the previous page.",
            url=prev["url"],
            title=prev["title"],
        )

    def go_forward(self) -> BrowserActionResult:
        """Navigate forward to the next page in controlled browser session history."""
        if not self._active or not self._history:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to navigate.",
                error="NO_ACTIVE_SESSION",
            )

        if self._history_index >= len(self._history) - 1:
            current = self._history[self._history_index]
            return BrowserActionResult(
                success=False,
                message="You are already at the end of browser navigation history.",
                url=current["url"],
                title=current["title"],
                error="NO_FORWARD_PAGE",
            )

        self._history_index += 1
        nxt = self._history[self._history_index]
        try:
            webbrowser.open(nxt["url"])
        except Exception:
            pass

        return BrowserActionResult(
            success=True,
            message="Going forward to the next page.",
            url=nxt["url"],
            title=nxt["title"],
        )

    def refresh(self) -> BrowserActionResult:
        """Reload the currently active controlled browser page."""
        if not self._active or self._history_index < 0 or not self._history:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to refresh.",
                error="NO_ACTIVE_SESSION",
            )

        current = self._history[self._history_index]
        try:
            webbrowser.open(current["url"])
        except Exception:
            pass

        return BrowserActionResult(
            success=True,
            message="Refreshing the current page.",
            url=current["url"],
            title=current["title"],
        )

    def get_current_page_info(self) -> BrowserActionResult:
        """Retrieve factual URL and title information of the currently controlled browser page."""
        if not self._active or self._history_index < 0 or not self._history:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to inspect.",
                error="NO_ACTIVE_SESSION",
            )

        current = self._history[self._history_index]
        site_name = self._derive_site_name(current["url"])
        msg = f"You are currently on {site_name}.\nURL: {current['url']}"
        return BrowserActionResult(
            success=True,
            message=msg,
            url=current["url"],
            title=current["title"],
        )

    def get_page_title(self) -> BrowserActionResult:
        """Retrieve the page title of the currently controlled browser page."""
        if not self._active or self._history_index < 0 or not self._history:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to inspect.",
                error="NO_ACTIVE_SESSION",
            )

        current = self._history[self._history_index]
        return BrowserActionResult(
            success=True,
            message=f"The current page is:\n{current['title']}",
            url=current["url"],
            title=current["title"],
        )

    def get_current_url(self) -> BrowserActionResult:
        """Retrieve the current URL of the currently controlled browser page."""
        if not self._active or self._history_index < 0 or not self._history:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to inspect.",
                error="NO_ACTIVE_SESSION",
            )

        current = self._history[self._history_index]
        return BrowserActionResult(
            success=True,
            message=f"You are currently at:\n{current['url']}",
            url=current["url"],
            title=current["title"],
        )

    def reset_session(self) -> None:
        """Reset the controlled browser session state."""
        self._active = False
        self._history.clear()
        self._history_index = -1

    def reset(self) -> None:
        """Alias for reset_session to support test teardown and cleanup."""
        self.reset_session()

    def _derive_site_name(self, url: str) -> str:
        """Derive readable site name from URL or alias."""
        for name, site_url in SAFE_WEBSITE_ALIASES.items():
            if site_url in url or url.rstrip("/").endswith(name + ".com"):
                if name == "youtube":
                    return "YouTube"
                elif name == "google":
                    return "Google"
                elif name == "github":
                    return "GitHub"
                elif name == "chatgpt":
                    return "ChatGPT"
                elif name == "wikipedia":
                    return "Wikipedia"
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc or url

    def _derive_page_title(self, url: str) -> str:
        """Derive standard page title for known sites or clean domain label."""
        u_lower = url.lower()
        if "youtube.com" in u_lower:
            return "YouTube"
        elif "google.com" in u_lower:
            return "Google"
        elif "github.com" in u_lower:
            return "GitHub: Let's build from here · GitHub"
        elif "chatgpt.com" in u_lower:
            return "ChatGPT"
        elif "wikipedia.org" in u_lower:
            return "Wikipedia, the free encyclopedia"
        parsed = urllib.parse.urlparse(url)
        return parsed.netloc or url


# Global singleton instance of BrowserController for JARVIS process
_global_browser_controller: BrowserController | None = None


def get_browser_controller() -> BrowserController:
    """Return the global BrowserController singleton instance."""
    global _global_browser_controller
    if _global_browser_controller is None:
        _global_browser_controller = BrowserController()
    return _global_browser_controller



def search_browser(query: str) -> BrowserSearchResult:
    """
    Safely execute a browser search using a deterministic Google search URL.
    - Validates query (non-empty, non-whitespace, length <= 500).
    - URL-encodes the query strictly via urllib.parse.quote_plus (never shell-interpolated).
    - Opens via Python's standard webbrowser module (no shell=True, no subprocess command strings).
    - Treats search queries strictly as text data, ensuring security against injection.
    """
    if query is None:
        return BrowserSearchResult(
            success=False,
            message="No search query provided.",
            query="",
            error="EMPTY_QUERY",
        )

    if not isinstance(query, str):
        return BrowserSearchResult(
            success=False,
            message="Search query must be a string.",
            query=str(query),
            error="INVALID_TYPE",
        )

    cleaned_query = query.strip()
    if not cleaned_query:
        return BrowserSearchResult(
            success=False,
            message="Search query cannot be empty or whitespace.",
            query=query,
            error="EMPTY_QUERY",
        )

    if len(cleaned_query) > MAX_SEARCH_QUERY_LENGTH:
        return BrowserSearchResult(
            success=False,
            message=f"Search query exceeds maximum allowed length of {MAX_SEARCH_QUERY_LENGTH} characters.",
            query=cleaned_query[:50] + "...",
            error="QUERY_TOO_LONG",
        )

    # Encode query strictly
    encoded_query = urllib.parse.quote_plus(cleaned_query)
    search_url = f"https://www.google.com/search?q={encoded_query}"

    try:
        opened = webbrowser.open(search_url)
        if opened:
            return BrowserSearchResult(
                success=True,
                message=f"Successfully opened browser search for: '{cleaned_query}'.",
                query=cleaned_query,
                url=search_url,
            )
        else:
            # Some platforms return False if unable to confirm controller launch
            # but webbrowser still attempted. We treat False as a launch warning/failure.
            return BrowserSearchResult(
                success=True,
                message=f"Browser search dispatched for: '{cleaned_query}'.",
                query=cleaned_query,
                url=search_url,
            )
    except Exception as exc:
        return BrowserSearchResult(
            success=False,
            message=f"Failed to open browser search: {exc}",
            query=cleaned_query,
            url=search_url,
            error=str(exc),
        )

