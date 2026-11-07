"""
Comprehensive tests for Phase 3: Real Computer Control, Browser Automation & Desktop Interaction.
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

from tools.desktop import DesktopController, get_desktop_controller, ALLOWED_KEYBOARD_KEYS
from tools.browser import get_browser_controller
from core.orchestrator import JarvisOrchestrator
from core.executor import PlanExecutor


class TestComputerControlPhase3(unittest.TestCase):
    def setUp(self):
        self.desktop_ctrl = get_desktop_controller()
        self.browser_ctrl = get_browser_controller()
        self.browser_ctrl.reset()
        mock_llm = MagicMock()
        self.orchestrator = JarvisOrchestrator(llm=mock_llm)

    def tearDown(self):
        self.browser_ctrl.reset()

    # -------------------------------------------------------------
    # 1. Desktop Controller Unit Tests
    # -------------------------------------------------------------

    def test_screen_dimensions(self):
        w, h = self.desktop_ctrl.get_screen_dimensions()
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)

    def test_mouse_position(self):
        x, y = self.desktop_ctrl.get_mouse_position()
        self.assertIsInstance(x, int)
        self.assertIsInstance(y, int)

    def test_move_mouse_valid_and_out_of_bounds(self):
        w, h = self.desktop_ctrl.get_screen_dimensions()
        # Valid move
        res_valid = self.desktop_ctrl.move_mouse(100, 100)
        self.assertTrue(res_valid.success)
        self.assertTrue(res_valid.verified)

        # Invalid out-of-bounds move
        res_invalid = self.desktop_ctrl.move_mouse(w + 1000, h + 1000)
        self.assertFalse(res_invalid.success)
        self.assertEqual(res_invalid.error, "OUT_OF_BOUNDS")

    def test_mouse_clicks(self):
        res_click = self.desktop_ctrl.click_mouse(100, 100, button="left", double=False)
        self.assertTrue(res_click.success)
        self.assertTrue(res_click.verified)

        res_double = self.desktop_ctrl.click_mouse(100, 100, button="left", double=True)
        self.assertTrue(res_double.success)

        res_right = self.desktop_ctrl.click_mouse(100, 100, button="right", double=False)
        self.assertTrue(res_right.success)

    def test_mouse_scroll(self):
        res_down = self.desktop_ctrl.scroll_mouse(direction="down", amount=3)
        self.assertTrue(res_down.success)

        res_up = self.desktop_ctrl.scroll_mouse(direction="up", amount=2)
        self.assertTrue(res_up.success)

    def test_type_text(self):
        res = self.desktop_ctrl.type_text("Hello World")
        self.assertTrue(res.success)
        self.assertEqual(res.data["length"], 11)

        # Empty text
        res_empty = self.desktop_ctrl.type_text("")
        self.assertFalse(res_empty.success)
        self.assertEqual(res_empty.error, "EMPTY_TEXT")

        # Exceeds limit
        long_text = "a" * 501
        res_long = self.desktop_ctrl.type_text(long_text)
        self.assertFalse(res_long.success)
        self.assertEqual(res_long.error, "TEXT_TOO_LONG")

    def test_press_key(self):
        res_enter = self.desktop_ctrl.press_key("enter")
        self.assertTrue(res_enter.success)

        res_esc = self.desktop_ctrl.press_key("esc")
        self.assertTrue(res_esc.success)

        # Unsupported key
        res_invalid = self.desktop_ctrl.press_key("invalid_key_xyz")
        self.assertFalse(res_invalid.success)
        self.assertEqual(res_invalid.error, "UNSUPPORTED_KEY")

    def test_hotkey(self):
        res_hotkey = self.desktop_ctrl.hotkey("ctrl", "a")
        self.assertTrue(res_hotkey.success)

        res_empty = self.desktop_ctrl.hotkey()
        self.assertFalse(res_empty.success)

        res_invalid = self.desktop_ctrl.hotkey("ctrl", "nonexistent_key_123")
        self.assertFalse(res_invalid.success)

    def test_capture_and_describe_screen(self):
        res_cap = self.desktop_ctrl.capture_screen(save=True)
        self.assertTrue(res_cap.success)
        self.assertTrue(res_cap.verified)
        self.assertIsNotNone(res_cap.data.get("path"))

        res_desc = self.desktop_ctrl.describe_screen()
        self.assertTrue(res_desc.success)
        self.assertIn("Screen captured successfully", res_desc.message)

    # -------------------------------------------------------------
    # 2. Browser Advanced Inspection Unit Tests
    # -------------------------------------------------------------

    @patch("webbrowser.open", return_value=True)
    def test_browser_page_content_and_links(self, mock_open):
        self.browser_ctrl.open_website("google")
        res_content = self.browser_ctrl.get_page_content()
        self.assertTrue(res_content.success)
        self.assertIn("Google", res_content.message)

        res_links = self.browser_ctrl.get_visible_links()
        self.assertTrue(res_links.success)
        self.assertIsNotNone(res_links.data.get("links"))

    def test_browser_inspection_without_session(self):
        res_content = self.browser_ctrl.get_page_content()
        self.assertFalse(res_content.success)
        self.assertEqual(res_content.error, "NO_ACTIVE_SESSION")

        res_links = self.browser_ctrl.get_visible_links()
        self.assertFalse(res_links.success)
        self.assertEqual(res_links.error, "NO_ACTIVE_SESSION")

    # -------------------------------------------------------------
    # 3. Orchestrator Integration Tests
    # -------------------------------------------------------------

    def test_orchestrator_capture_screen(self):
        res = self.orchestrator.handle("Take a screenshot")
        self.assertEqual(res.plan.actions[0].tool, "capture_screen")
        self.assertTrue(res.execution_result.results[0].success)

    def test_orchestrator_describe_screen(self):
        res = self.orchestrator.handle("What is on my screen?")
        self.assertEqual(res.plan.actions[0].tool, "describe_screen")
        self.assertTrue(res.execution_result.results[0].success)
        self.assertIn("Screen captured successfully", res.message)

    def test_orchestrator_move_mouse(self):
        res = self.orchestrator.handle("Move mouse to 500, 400")
        self.assertEqual(res.plan.actions[0].tool, "move_mouse")
        self.assertEqual(res.plan.actions[0].arguments, {"x": 500, "y": 400})
        self.assertTrue(res.execution_result.results[0].success)

    def test_orchestrator_click_mouse(self):
        res = self.orchestrator.handle("Click mouse")
        self.assertEqual(res.plan.actions[0].tool, "click_mouse")
        self.assertTrue(res.execution_result.results[0].success)

    def test_orchestrator_scroll_mouse(self):
        res = self.orchestrator.handle("Scroll down")
        self.assertEqual(res.plan.actions[0].tool, "scroll_mouse")
        self.assertTrue(res.execution_result.results[0].success)

    def test_orchestrator_type_text(self):
        res = self.orchestrator.handle("Type Hello World")
        self.assertEqual(res.plan.actions[0].tool, "type_text")
        self.assertEqual(res.plan.actions[0].arguments["text"], "Hello World")
        self.assertTrue(res.execution_result.results[0].success)

    def test_orchestrator_press_key(self):
        res = self.orchestrator.handle("Press Enter")
        self.assertEqual(res.plan.actions[0].tool, "press_key")
        self.assertEqual(res.plan.actions[0].arguments["key"], "enter")
        self.assertTrue(res.execution_result.results[0].success)

    def test_orchestrator_hotkey(self):
        res = self.orchestrator.handle("Press Ctrl+A")
        self.assertEqual(res.plan.actions[0].tool, "hotkey")
        self.assertEqual(res.plan.actions[0].arguments["keys"], ["ctrl", "a"])
        self.assertTrue(res.execution_result.results[0].success)

    @patch("webbrowser.open", return_value=True)
    def test_orchestrator_browser_content_and_links(self, mock_open):
        self.orchestrator.handle("Open Google")
        res_content = self.orchestrator.handle("What content is visible?")
        self.assertEqual(res_content.plan.actions[0].tool, "browser_get_page_content")
        self.assertTrue(res_content.execution_result.results[0].success)

        res_links = self.orchestrator.handle("What links are visible?")
        self.assertEqual(res_links.plan.actions[0].tool, "browser_get_visible_links")
        self.assertTrue(res_links.execution_result.results[0].success)

    def test_orchestrator_multi_step_computer_control(self):
        res = self.orchestrator.handle("Take a screenshot and then move mouse to 500, 300")
        self.assertEqual(len(res.plan.actions), 2)
        self.assertEqual(res.plan.actions[0].tool, "capture_screen")
        self.assertEqual(res.plan.actions[1].tool, "move_mouse")
        self.assertTrue(res.execution_result.results[0].success)
        self.assertTrue(res.execution_result.results[1].success)
