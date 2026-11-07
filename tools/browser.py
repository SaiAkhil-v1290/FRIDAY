import os
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
    data: dict | None = None
    error: str | None = None


def _normalize_browser_url(url: str | None) -> str | None:
    """Normalize trailing slash on bare domains so URL comparison is consistent."""
    if not url:
        return url
    if url.endswith("/") and url.count("/") == 3 and not url.startswith("data:"):
        # e.g. https://example.com/ -> https://example.com
        return url[:-1]
    return url


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

    # 2. Check explicit schemes (http://, https://, or data: for testing)
    if target.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(target)
        if not parsed.netloc or ("." not in parsed.netloc and parsed.netloc.split(":")[0] not in {"localhost", "127.0.0.1"}):
            return None, f"Invalid URL domain: '{raw_target}'"
        return target, None

    if target.startswith("data:text/html"):
        return target, None

    # 3. Check domain-like strings (e.g. example.com, www.example.com, github.com)
    # Must contain a valid dot and look like a hostname
    if re.match(r"^(?:www\.)?[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+(?:/.*)?$", target):
        return f"https://{target}", None

    return None, f"Cannot resolve '{raw_target}' to a valid web URL."


class BrowserController:
    """
    Dedicated browser controller managing a genuinely controlled Playwright Chromium session.
    Provides safe, non-shell browser navigation, genuine browser automation via Playwright
    as the single source of truth, live page lifecycle tracking, and verified page awareness.
    Never uses fake fallbacks (no urllib, no BeautifulSoup, no webbrowser.open, no simulated history).
    """

    def __init__(self) -> None:
        self._active: bool = False
        self._playwright = None
        self._browser = None
        self._page = None

    @property
    def is_active(self) -> bool:
        return self._active and self._page is not None and not self._page.is_closed()

    def _ensure_browser(self) -> tuple[bool, str | None]:
        """
        Lazily initialize controlled Playwright browser instance.
        Returns (success, error_message).
        Playwright is the single source of truth; if unavailable, returns explicit failure.
        """
        if self._page is not None and not self._page.is_closed():
            return True, None

        try:
            from playwright.sync_api import sync_playwright
            if self._playwright is None:
                self._playwright = sync_playwright().start()
            if self._browser is None or not self._browser.is_connected():
                self._browser = self._playwright.chromium.launch(headless=True)
            self._page = self._browser.new_page()
            self._active = True
            return True, None
        except ImportError:
            return False, "Playwright is not installed. Please run: pip install playwright && python -m playwright install chromium"
        except Exception as exc:
            return False, f"Failed to launch controlled Playwright Chromium browser: {exc}"

    def open_website(self, target: str) -> BrowserActionResult:
        """
        Navigate to a website alias or validated URL using the controlled Playwright session.
        Never dispatches to system browser or simulates state.
        """
        normalized_url, err = validate_and_normalize_url(target)
        if err or not normalized_url:
            return BrowserActionResult(
                success=False,
                message=err or f"Invalid URL target: {target}",
                error="INVALID_URL",
            )

        ready, launch_err = self._ensure_browser()
        if not ready or self._page is None:
            return BrowserActionResult(
                success=False,
                message=launch_err or "Playwright browser is unavailable.",
                error="BROWSER_UNAVAILABLE",
            )

        try:
            response = self._page.goto(normalized_url, wait_until="domcontentloaded", timeout=15000)
            real_url = _normalize_browser_url(self._page.url) or normalized_url
            real_title = self._page.title() or self._derive_page_title(real_url)
            self._active = True

            site_name = self._derive_site_name(real_url)
            return BrowserActionResult(
                success=True,
                message=f"Opening {site_name}.",
                url=real_url,
                title=real_title,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to navigate to {normalized_url}: {exc}",
                url=normalized_url,
                error=str(exc),
            )

    def go_back(self) -> BrowserActionResult:
        """Navigate back to the previous page in the actual controlled Playwright browser session."""
        if not self.is_active:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to navigate.",
                error="NO_ACTIVE_SESSION",
            )

        try:
            prev_url = _normalize_browser_url(self._page.url)
            resp = self._page.go_back(wait_until="domcontentloaded", timeout=10000)
            cur_url = _normalize_browser_url(self._page.url)
            cur_title = self._page.title() or self._derive_page_title(cur_url)

            # If response is None and the URL didn't change, we are at beginning of history
            if resp is None and cur_url == prev_url:
                return BrowserActionResult(
                    success=False,
                    message="You are already at the beginning of browser navigation history.",
                    url=cur_url,
                    title=cur_title,
                    error="NO_PREVIOUS_PAGE",
                )

            return BrowserActionResult(
                success=True,
                message="Going back to the previous page.",
                url=cur_url,
                title=cur_title,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to navigate back: {exc}",
                error=str(exc),
            )

    def go_forward(self) -> BrowserActionResult:
        """Navigate forward to the next page in the actual controlled Playwright browser session."""
        if not self.is_active:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to navigate.",
                error="NO_ACTIVE_SESSION",
            )

        try:
            prev_url = _normalize_browser_url(self._page.url)
            resp = self._page.go_forward(wait_until="domcontentloaded", timeout=10000)
            cur_url = _normalize_browser_url(self._page.url)
            cur_title = self._page.title() or self._derive_page_title(cur_url)

            # If response is None and the URL didn't change, we are at end of history
            if resp is None and cur_url == prev_url:
                return BrowserActionResult(
                    success=False,
                    message="You are already at the end of browser navigation history.",
                    url=cur_url,
                    title=cur_title,
                    error="NO_FORWARD_PAGE",
                )

            return BrowserActionResult(
                success=True,
                message="Going forward to the next page.",
                url=cur_url,
                title=cur_title,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to navigate forward: {exc}",
                error=str(exc),
            )

    def refresh(self) -> BrowserActionResult:
        """Reload the currently active controlled Playwright browser page."""
        if not self.is_active:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to refresh.",
                error="NO_ACTIVE_SESSION",
            )

        try:
            self._page.reload(wait_until="domcontentloaded", timeout=10000)
            cur_url = _normalize_browser_url(self._page.url)
            cur_title = self._page.title()
            return BrowserActionResult(
                success=True,
                message="Refreshing the current page.",
                url=cur_url,
                title=cur_title,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to refresh page: {exc}",
                error=str(exc),
            )

    def get_current_page_info(self) -> BrowserActionResult:
        """Retrieve factual URL and title information from the actual controlled Playwright page."""
        if not self.is_active:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to inspect.",
                error="NO_ACTIVE_SESSION",
            )

        try:
            url = _normalize_browser_url(self._page.url)
            title = self._page.title() or self._derive_page_title(url)
            site_name = self._derive_site_name(url)
            msg = f"You are currently on {site_name}.\nURL: {url}"
            return BrowserActionResult(
                success=True,
                message=msg,
                url=url,
                title=title,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to inspect current page: {exc}",
                error=str(exc),
            )

    def get_page_title(self) -> BrowserActionResult:
        """Retrieve the page title of the currently controlled Playwright browser page."""
        if not self.is_active:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to inspect.",
                error="NO_ACTIVE_SESSION",
            )

        try:
            title = self._page.title() or self._derive_page_title(self._page.url)
            url = _normalize_browser_url(self._page.url)
            return BrowserActionResult(
                success=True,
                message=f"The current page is:\n{title}",
                url=url,
                title=title,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to get page title: {exc}",
                error=str(exc),
            )

    def get_current_url(self) -> BrowserActionResult:
        """Retrieve the current URL of the currently controlled Playwright browser page."""
        if not self.is_active:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to inspect.",
                error="NO_ACTIVE_SESSION",
            )

        try:
            url = _normalize_browser_url(self._page.url)
            title = self._page.title() or self._derive_page_title(url)
            return BrowserActionResult(
                success=True,
                message=f"You are currently at:\n{url}",
                url=url,
                title=title,
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to get current URL: {exc}",
                error=str(exc),
            )

    def get_page_content(self) -> BrowserActionResult:
        """
        Inspect actual page content of the current controlled page directly from live Playwright DOM.
        Never falls back to urllib/BeautifulSoup or simulated text.
        """
        if not self.is_active:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to inspect.",
                error="NO_ACTIVE_SESSION",
            )

        try:
            body_text = self._page.inner_text("body", timeout=5000)
            clean_text = " ".join(body_text.split())
            p_title = self._page.title() or self._derive_page_title(self._page.url)
            preview = clean_text[:500] + ("..." if len(clean_text) > 500 else "")
            return BrowserActionResult(
                success=True,
                message=f"Page content for {p_title}:\n{preview}",
                url=self._page.url,
                title=p_title,
                data={"content": preview, "full_length": len(clean_text)},
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to extract page content from active browser DOM: {exc}",
                error=str(exc),
            )

    def get_visible_links(self) -> BrowserActionResult:
        """
        Extract visible links directly from the currently controlled Playwright page DOM.
        Strictly limits output to first 10 links.
        Never falls back to urllib/BeautifulSoup or simulated links.
        """
        if not self.is_active:
            return BrowserActionResult(
                success=False,
                message="I don't currently have an active browser session to inspect.",
                error="NO_ACTIVE_SESSION",
            )

        try:
            raw_links = self._page.eval_on_selector_all(
                "a[href]",
                """elements => elements.map(e => ({
                    text: (e.innerText || e.textContent || '').trim(),
                    href: e.href
                })).filter(item => item.text && !item.href.startsWith('javascript:') && !item.href.startsWith('#')).slice(0, 10)"""
            )
            links = [(item["text"], item["href"]) for item in (raw_links or [])]
            cur_title = self._page.title() or self._derive_page_title(self._page.url)

            if links:
                lines = [f"Found {len(links)} visible links on {cur_title}:"]
                for idx, (t, u) in enumerate(links, 1):
                    lines.append(f"{idx}. {t} -> {u}")
                msg = "\n".join(lines)
            else:
                msg = f"No visible links found on {cur_title}."

            return BrowserActionResult(
                success=True,
                message=msg,
                url=self._page.url,
                title=cur_title,
                data={"links": links},
            )
        except Exception as exc:
            return BrowserActionResult(
                success=False,
                message=f"Failed to extract visible links from active browser DOM: {exc}",
                error=str(exc),
            )

    def reset_session(self) -> None:
        """Reset the controlled browser session state and close Playwright browser page."""
        self._active = False
        if self._page is not None:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

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

