"""
Unit and integration tests for Milestone 5.1: Browser Automation Foundation & Page Awareness.
"""

import unittest
from unittest.mock import patch, MagicMock

from tools.browser import (
    BrowserController,
    get_browser_controller,
    validate_and_normalize_url,
    BrowserActionResult,
)
from tools.application import ApplicationLaunchResult
from core.orchestrator import JarvisOrchestrator


class TestBrowserAutomation(unittest.TestCase):
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

    @patch("webbrowser.open", return_value=True)
    def test_browser_controller_navigation_history(self, mock_open):
        res1 = self.controller.open_website("https://example.com")
        self.assertTrue(res1.success)
        self.assertEqual(self.controller.get_current_url().url, "https://example.com")
        self.assertEqual(self.controller.get_page_title().title, "example.com")

        res2 = self.controller.open_website("https://github.com")
        self.assertTrue(res2.success)
        self.assertEqual(self.controller.get_current_url().url, "https://github.com")
        self.assertIn("GitHub", self.controller.get_page_title().title)

        # Go back
        res_back = self.controller.go_back()
        self.assertTrue(res_back.success)
        self.assertEqual(self.controller.get_current_url().url, "https://example.com")

        # Go forward
        res_fwd = self.controller.go_forward()
        self.assertTrue(res_fwd.success)
        self.assertEqual(self.controller.get_current_url().url, "https://github.com")

        # Refresh
        res_ref = self.controller.refresh()
        self.assertTrue(res_ref.success)
        self.assertEqual(self.controller.get_current_url().url, "https://github.com")

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
    # Orchestrator Integration Tests
    # -------------------------------------------------------------

    @patch("webbrowser.open", return_value=True)
    def test_navigate_named_website(self, mock_open):
        # User: Open YouTube
        res = self.orchestrator.handle("Open YouTube")
        self.assertEqual(res.plan.intent, "browser_navigate")
        self.assertEqual(res.plan.actions[0].tool, "browser_navigate")
        self.assertEqual(res.plan.actions[0].arguments["url"], "https://www.youtube.com")
        self.assertTrue(res.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, "https://www.youtube.com")

    @patch("webbrowser.open", return_value=True)
    def test_navigate_direct_url(self, mock_open):
        # User: Open https://example.com
        res = self.orchestrator.handle("Open https://example.com")
        self.assertEqual(res.plan.intent, "browser_navigate")
        self.assertEqual(res.plan.actions[0].tool, "browser_navigate")
        self.assertEqual(res.plan.actions[0].arguments["url"], "https://example.com")
        self.assertTrue(res.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, "https://example.com")

    @patch("webbrowser.open", return_value=True)
    def test_navigate_domain_normalization(self, mock_open):
        # User: Go to example.com
        res = self.orchestrator.handle("Go to example.com")
        self.assertEqual(res.plan.intent, "browser_navigate")
        self.assertEqual(res.plan.actions[0].tool, "browser_navigate")
        self.assertEqual(res.plan.actions[0].arguments["url"], "https://example.com")
        self.assertEqual(self.controller.get_current_url().url, "https://example.com")

    @patch("webbrowser.open", return_value=True)
    def test_page_awareness_when_active(self, mock_open):
        self.orchestrator.handle("Open GitHub")

        # What page am I on?
        res1 = self.orchestrator.handle("What page am I on?")
        self.assertIn("You are currently on GitHub", res1.message)
        self.assertIn("https://github.com", res1.message)
        self.assertTrue(res1.execution_result.results[0].success)

        # What is the title of this page?
        res2 = self.orchestrator.handle("What is the title of this page?")
        self.assertIn("GitHub", res2.message)
        self.assertTrue(res2.execution_result.results[0].success)

        # What URL am I on?
        res3 = self.orchestrator.handle("What URL am I on?")
        self.assertIn("https://github.com", res3.message)
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

    @patch("webbrowser.open", return_value=True)
    def test_go_back_and_forward(self, mock_open):
        self.orchestrator.handle("Open Google")
        self.orchestrator.handle("Open GitHub")
        self.assertEqual(self.controller.get_current_url().url, "https://github.com")

        # Go back
        res_back = self.orchestrator.handle("Go back")
        self.assertEqual(res_back.plan.actions[0].tool, "browser_go_back")
        self.assertTrue(res_back.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, "https://www.google.com")

        # Go forward
        res_fwd = self.orchestrator.handle("Go forward")
        self.assertEqual(res_fwd.plan.actions[0].tool, "browser_go_forward")
        self.assertTrue(res_fwd.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, "https://github.com")

    @patch("webbrowser.open", return_value=True)
    def test_refresh_page(self, mock_open):
        self.orchestrator.handle("Open YouTube")
        res_ref = self.orchestrator.handle("Refresh the page")
        self.assertEqual(res_ref.plan.actions[0].tool, "browser_refresh")
        self.assertTrue(res_ref.execution_result.results[0].success)
        self.assertEqual(self.controller.get_current_url().url, "https://www.youtube.com")

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

    @patch("webbrowser.open", return_value=True)
    def test_search_protection_over_browser_control_words(self, mock_open):
        # "Search for how to go back in Chrome" MUST resolve to browser_search, NOT browser_go_back
        res = self.orchestrator.handle("Search for how to go back in Chrome")
        self.assertEqual(res.plan.intent, "browser_search")
        self.assertEqual(res.plan.actions[0].tool, "browser_search")
        self.assertEqual(res.plan.actions[0].arguments["query"], "how to go back in Chrome")

    @patch("webbrowser.open", return_value=True)
    def test_multi_step_browser_navigation_and_refresh(self, mock_open):
        res = self.orchestrator.handle("Open Google and then refresh the page")
        self.assertEqual(len(res.plan.actions), 2)
        self.assertEqual(res.plan.actions[0].tool, "browser_navigate")
        self.assertEqual(res.plan.actions[1].tool, "browser_refresh")
        self.assertTrue(res.execution_result.results[0].success)
        self.assertTrue(res.execution_result.results[1].success)
        self.assertEqual(self.controller.get_current_url().url, "https://www.google.com")

    def test_multi_step_browser_invalid_url_stops_execution(self):
        # Multi-step with invalid target fails on step 1, subsequent steps skipped
        res = self.orchestrator.handle("Open UnknownApp and then refresh the page")
        self.assertEqual(len(res.plan.actions), 2)
        self.assertEqual(res.plan.actions[0].tool, "open_application")
        self.assertFalse(res.execution_result.results[0].success)
        self.assertFalse(res.execution_result.results[1].executed)
        self.assertEqual(res.execution_result.results[1].error, "PREVIOUS_ACTION_FAILED")
