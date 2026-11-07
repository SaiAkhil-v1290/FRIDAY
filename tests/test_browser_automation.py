"""
Unit and integration tests for Milestone 5.1 and Phase 3.3:
Browser Automation Foundation, Live Page Awareness, and Local Test Server.
All browser navigation tests use a local in-process HTTP test server to ensure
zero dependencies on external websites or internet access.
"""

import unittest
from unittest.mock import patch, MagicMock
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from tools.browser import (
    BrowserController,
    get_browser_controller,
    validate_and_normalize_url,
    BrowserActionResult,
)
from tools.application import ApplicationLaunchResult
from core.orchestrator import JarvisOrchestrator


class MockHTTPHandler(BaseHTTPRequestHandler):
    """Local HTTP handler serving predictable HTML pages for testing."""

    def log_message(self, format, *args):
        pass  # Suppress console logging

    def _send_html(self, status: int, html_str: str):
        content = html_str.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path in {"/", "/page1"}:
            html = """<!DOCTYPE html>
<html>
<head><title>Local Test Page One</title></head>
<body>
    <h1>Welcome to Page One</h1>
    <p>This is a local test environment for FRIDAY browser control.</p>
    <div>
        <a href="/page2">Next Local Page</a>
        <a href="/hidden" style="display:none">Hidden Display Link</a>
        <a href="/vis_hidden" style="visibility:hidden">Hidden Vis Link</a>
        <div style="display:none"><a href="/parent_hidden">Parent Hidden Link</a></div>
        <a href="/empty">   </a>
    </div>
</body>
</html>"""
            self._send_html(200, html)

        elif self.path == "/page2":
            html = """<!DOCTYPE html>
<html>
<head><title>Local Test Page Two</title></head>
<body>
    <h1>Second Local Page</h1>
    <p>Navigated successfully to page two.</p>
    <a href="/page1">Back to Page One</a>
</body>
</html>"""
            self._send_html(200, html)

        elif self.path == "/page3":
            html = """<!DOCTYPE html>
<html>
<head><title>Local Test Page Three</title></head>
<body>
    <h1>Third Local Page</h1>
</body>
</html>"""
            self._send_html(200, html)

        elif self.path == "/not_found":
            self._send_html(404, "<h1>404 Not Found</h1>")

        elif self.path == "/server_error":
            self._send_html(500, "<h1>500 Internal Server Error</h1>")

        else:
            self._send_html(404, "<h1>Not Found</h1>")


class TestBrowserAutomation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Start in-process local HTTP server on random free port
        cls.server = HTTPServer(("127.0.0.1", 0), MockHTTPHandler)
        cls.port = cls.server.server_port
        cls.base_url = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        get_browser_controller().reset_session()

    def setUp(self):
        self.controller = get_browser_controller()
        self.controller.reset()
        mock_llm = MagicMock()
        self.orchestrator = JarvisOrchestrator(llm=mock_llm)

    def tearDown(self):
        self.controller.reset()

    # -------------------------------------------------------------
    # Controller Unit Tests
    # -------------------------------------------------------------

    def test_url_validation_and_normalization(self):
        # Valid URLs & Domains
        norm, err = validate_and_normalize_url("https://example.com")
        self.assertEqual(norm, "https://example.com")
        self.assertIsNone(err)

        norm, err = validate_and_normalize_url("http://example.com/test")
        self.assertEqual(norm, "http://example.com/test")
        self.assertIsNone(err)

        norm, err = validate_and_normalize_url("example.com")
        self.assertEqual(norm, "https://example.com")
        self.assertIsNone(err)

        norm, err = validate_and_normalize_url("google.com")
        self.assertEqual(norm, "https://google.com")
        self.assertIsNone(err)

        norm, err = validate_and_normalize_url("github.com/repo")
        self.assertEqual(norm, "https://github.com/repo")
        self.assertIsNone(err)

        norm, err = validate_and_normalize_url("youtube")
        self.assertEqual(norm, "https://www.youtube.com")
        self.assertIsNone(err)

        norm, err = validate_and_normalize_url("google")
        self.assertEqual(norm, "https://www.google.com")
        self.assertIsNone(err)

        # Localhost validation
        norm, err = validate_and_normalize_url(f"http://127.0.0.1:{self.port}/page1")
        self.assertEqual(norm, f"http://127.0.0.1:{self.port}/page1")
        self.assertIsNone(err)

        # Invalid / Dangerous inputs
        norm, err = validate_and_normalize_url("cmd.exe")
        self.assertIsNone(norm)
        self.assertIsNotNone(err)

        norm, err = validate_and_normalize_url("C:\\Windows\\System32")
        self.assertIsNone(norm)
        self.assertIsNotNone(err)

        norm, err = validate_and_normalize_url("/usr/bin/bash")
        self.assertIsNone(norm)
        self.assertIsNotNone(err)

        norm, err = validate_and_normalize_url("rm -rf /")
        self.assertIsNone(norm)
        self.assertIsNotNone(err)

        norm, err = validate_and_normalize_url("")
        self.assertIsNone(norm)
        self.assertIsNotNone(err)

        norm, err = validate_and_normalize_url("not a domain at all")
        self.assertIsNone(norm)
        self.assertIsNotNone(err)

    def test_browser_controller_local_navigation_history(self):
        url1 = f"{self.base_url}/page1"
        url2 = f"{self.base_url}/page2"

        res1 = self.controller.open_website(url1)
        self.assertTrue(res1.success)
        self.assertEqual(self.controller.get_current_url().url, url1)
        self.assertIn("Local Test Page One", self.controller.get_page_title().title)

        res2 = self.controller.open_website(url2)
        self.assertTrue(res2.success)
        self.assertEqual(self.controller.get_current_url().url, url2)
        self.assertIn("Local Test Page Two", self.controller.get_page_title().title)

        # Go back
        res_back = self.controller.go_back()
        self.assertTrue(res_back.success)
        self.assertEqual(self.controller.get_current_url().url, url1)

        # Go forward
        res_fwd = self.controller.go_forward()
        self.assertTrue(res_fwd.success)
        self.assertEqual(self.controller.get_current_url().url, url2)

        # Refresh
        res_ref = self.controller.refresh()
        self.assertTrue(res_ref.success)
        self.assertEqual(self.controller.get_current_url().url, url2)

    def test_browser_controller_no_session(self):
        # Empty session
        self.assertFalse(self.controller.is_active)
        self.assertFalse(self.controller.get_current_url().success)
        self.assertFalse(self.controller.get_page_title().success)

        # Back/forward/refresh with no session
        res_back = self.controller.go_back()
        self.assertFalse(res_back.success)
        self.assertEqual(res_back.error, "NO_ACTIVE_SESSION")

        res_fwd = self.controller.go_forward()
        self.assertFalse(res_fwd.success)
        self.assertEqual(res_fwd.error, "NO_ACTIVE_SESSION")

        res_ref = self.controller.refresh()
        self.assertFalse(res_ref.success)
        self.assertEqual(res_ref.error, "NO_ACTIVE_SESSION")

    # -------------------------------------------------------------
    # Orchestrator Integration Tests (Local HTTP Server)
    # -------------------------------------------------------------

    def test_navigate_direct_url_local(self):
        url = f"{self.base_url}/page1"
        res = self.orchestrator.handle(f"Open {url}")
        self.assertEqual(res.plan.intent, "browser_navigate")
        self.assertEqual(res.plan.actions[0].tool, "browser_navigate")
        self.assertTrue(res.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, url)

    def test_page_awareness_when_active_local(self):
        url = f"{self.base_url}/page1"
        self.orchestrator.handle(f"Open {url}")

        # What page am I on?
        res1 = self.orchestrator.handle("What page am I on?")
        self.assertIn("127.0.0.1", res1.message)
        self.assertIn(url, res1.message)
        self.assertTrue(res1.execution_result.results[0].success)

        # What is the title of this page?
        res2 = self.orchestrator.handle("What is the title of this page?")
        self.assertIn("Local Test Page One", res2.message)
        self.assertTrue(res2.execution_result.results[0].success)

        # What URL am I on?
        res3 = self.orchestrator.handle("What URL am I on?")
        self.assertIn(url, res3.message)
        self.assertTrue(res3.execution_result.results[0].success)

    def test_page_awareness_no_active_session(self):
        # Without navigating first
        res = self.orchestrator.handle("What page am I on?")
        self.assertIn("I don't currently have an active browser session to inspect.", res.message)
        self.assertFalse(res.execution_result.results[0].success)

        res_title = self.orchestrator.handle("What is the title of this page?")
        self.assertIn("I don't currently have an active browser session to inspect.", res_title.message)

        res_url = self.orchestrator.handle("What URL am I on?")
        self.assertIn("I don't currently have an active browser session to inspect.", res_url.message)

    def test_go_back_and_forward_local(self):
        url1 = f"{self.base_url}/page1"
        url2 = f"{self.base_url}/page2"

        self.orchestrator.handle(f"Open {url1}")
        self.orchestrator.handle(f"Open {url2}")
        self.assertEqual(self.controller.get_current_url().url, url2)

        # Go back
        res_back = self.orchestrator.handle("Go back")
        self.assertEqual(res_back.plan.actions[0].tool, "browser_go_back")
        self.assertTrue(res_back.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, url1)

        # Go forward
        res_fwd = self.orchestrator.handle("Go forward")
        self.assertEqual(res_fwd.plan.actions[0].tool, "browser_go_forward")
        self.assertTrue(res_fwd.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, url2)

    def test_refresh_page_local(self):
        url = f"{self.base_url}/page1"
        self.orchestrator.handle(f"Open {url}")
        res_ref = self.orchestrator.handle("Refresh the page")
        self.assertEqual(res_ref.plan.actions[0].tool, "browser_refresh")
        self.assertTrue(res_ref.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, url)

    def test_visible_links_detection_local(self):
        url = f"{self.base_url}/page1"
        self.orchestrator.handle(f"Open {url}")
        res_links = self.orchestrator.handle("What links are visible?")
        self.assertEqual(res_links.plan.actions[0].tool, "browser_get_visible_links")
        self.assertTrue(res_links.execution_result.results[0].success)

        # Must find exactly 1 genuinely visible link ("Next Local Page")
        # Hidden links (display:none, visibility:hidden, parent display:none, empty text) must be excluded
        links = res_links.execution_result.results[0].message
        self.assertIn("Next Local Page", links)
        self.assertNotIn("Hidden Display Link", links)
        self.assertNotIn("Hidden Vis Link", links)
        self.assertNotIn("Parent Hidden Link", links)

    def test_http_error_handling_local(self):
        # 404 error page handling
        url_404 = f"{self.base_url}/not_found"
        res_404 = self.controller.open_website(url_404)
        self.assertFalse(res_404.success)
        self.assertEqual(res_404.error, "HTTP_404")
        self.assertIn("HTTP 404 error", res_404.message)

        # 500 error page handling
        url_500 = f"{self.base_url}/server_error"
        res_500 = self.controller.open_website(url_500)
        self.assertFalse(res_500.success)
        self.assertEqual(res_500.error, "HTTP_500")
        self.assertIn("HTTP 500 error", res_500.message)

    # -------------------------------------------------------------
    # Safety & Disambiguation Regression Tests
    # -------------------------------------------------------------

    def test_open_chrome_launches_application_not_website(self):
        # "Open Chrome" MUST resolve to open_application, NOT browser_navigate
        with patch("core.executor.launch_application") as mock_launch:
            mock_launch.return_value = ApplicationLaunchResult(
                success=True,
                message="Successfully launched Google Chrome.",
                application="Google Chrome",
            )
            res = self.orchestrator.handle("Open Chrome")
            self.assertEqual(res.plan.actions[0].tool, "open_application")
            self.assertEqual(res.plan.actions[0].arguments["application"], "Google Chrome")
            self.assertFalse(self.controller.is_active)

    def test_search_protection_over_browser_control_words(self):
        # "Search for how to go back in Chrome" MUST resolve to browser_search, NOT browser_go_back
        res = self.orchestrator.handle("Search for how to go back in Chrome")
        self.assertEqual(res.plan.intent, "browser_search")
        self.assertEqual(res.plan.actions[0].tool, "browser_search")
        self.assertEqual(res.plan.actions[0].arguments["query"], "how to go back in Chrome")

    def test_multi_step_browser_navigation_and_refresh_local(self):
        url = f"{self.base_url}/page1"
        res = self.orchestrator.handle(f"Open {url} and then refresh the page")
        self.assertEqual(len(res.plan.actions), 2)
        self.assertEqual(res.plan.actions[0].tool, "browser_navigate")
        self.assertEqual(res.plan.actions[1].tool, "browser_refresh")
        self.assertTrue(res.execution_result.results[0].success)
        self.assertTrue(res.execution_result.results[1].success)
        self.assertEqual(self.controller.get_current_url().url, url)

    def test_multi_step_browser_invalid_url_stops_execution(self):
        # Multi-step with invalid target fails on step 1, subsequent steps skipped
        res = self.orchestrator.handle("Open UnknownApp and then refresh the page")
        self.assertEqual(len(res.plan.actions), 2)
        self.assertEqual(res.plan.actions[0].tool, "open_application")
        self.assertFalse(res.execution_result.results[0].success)
        self.assertFalse(res.execution_result.results[1].executed)
        self.assertEqual(res.execution_result.results[1].error, "PREVIOUS_ACTION_FAILED")

