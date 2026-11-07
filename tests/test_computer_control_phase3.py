"""
Comprehensive tests for Phase 3 and Phase 3.1:
Real Computer Control, Hardened Browser Automation & Visual Awareness.
All Windows API input dispatches are strictly mocked in unit tests to ensure
no real mouse movements or keystrokes occur during test execution.
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
from PIL import Image

from tools.desktop import (
    DesktopController,
    get_desktop_controller,
    ALLOWED_KEYBOARD_KEYS,
    ALLOWED_MOUSE_BUTTONS,
    ALLOWED_SCROLL_DIRECTIONS,
)
from tools.browser import get_browser_controller
from core.orchestrator import JarvisOrchestrator
from core.executor import PlanExecutor
from ai.schemas import TaskPlan, ProposedAction


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
    # 1. Desktop Controller Unit Tests (Mocked API calls for safety)
    # -------------------------------------------------------------

    @patch("ctypes.windll.user32.GetSystemMetrics")
    def test_screen_dimensions(self, mock_metrics):
        mock_metrics.side_effect = lambda code: 1920 if code == 0 else 1080
        w, h = self.desktop_ctrl.get_screen_dimensions()
        self.assertEqual(w, 1920)
        self.assertEqual(h, 1080)

    @patch("ctypes.windll.user32.GetCursorPos")
    def test_mouse_position(self, mock_get_pos):
        def fill_point(pt_ref):
            pt_ref._obj.x = 250
            pt_ref._obj.y = 350
            return 1
        mock_get_pos.side_effect = fill_point
        x, y = self.desktop_ctrl.get_mouse_position()
        self.assertEqual(x, 250)
        self.assertEqual(y, 350)

    @patch("ctypes.windll.user32.GetSystemMetrics")
    @patch("ctypes.windll.user32.SetCursorPos")
    @patch("tools.desktop.DesktopController.get_mouse_position")
    def test_move_mouse_valid_and_out_of_bounds(self, mock_get_pos, mock_set_pos, mock_metrics):
        mock_metrics.side_effect = lambda code: 1920 if code == 0 else 1080
        mock_set_pos.return_value = 1
        mock_get_pos.return_value = (100, 100)

        # Valid move
        res_valid = self.desktop_ctrl.move_mouse(100, 100)
        self.assertTrue(res_valid.success)
        self.assertTrue(res_valid.verified)
        mock_set_pos.assert_called_with(100, 100)

        # Invalid out-of-bounds move
        res_invalid = self.desktop_ctrl.move_mouse(3000, 2000)
        self.assertFalse(res_valid == res_invalid)
        self.assertFalse(res_invalid.success)
        self.assertEqual(res_invalid.error, "OUT_OF_BOUNDS")

        # Invalid coordinate types
        res_bad_coords = self.desktop_ctrl.move_mouse("invalid", 100)
        self.assertFalse(res_bad_coords.success)
        self.assertEqual(res_bad_coords.error, "INVALID_COORDINATES")

    @patch("ctypes.windll.user32.mouse_event")
    @patch("tools.desktop.DesktopController.move_mouse")
    def test_mouse_clicks_and_validation(self, mock_move, mock_mouse_event):
        mock_move.return_value = MagicMock(success=True)

        # Left click
        res_click = self.desktop_ctrl.click_mouse(100, 100, button="left", double=False)
        self.assertTrue(res_click.success)
        self.assertTrue(res_click.verified)
        self.assertTrue(mock_mouse_event.called)

        # Double click
        mock_mouse_event.reset_mock()
        res_double = self.desktop_ctrl.click_mouse(100, 100, button="left", double=True)
        self.assertTrue(res_double.success)
        self.assertEqual(mock_mouse_event.call_count, 4)  # 2 down, 2 up

        # Right click
        res_right = self.desktop_ctrl.click_mouse(100, 100, button="right", double=False)
        self.assertTrue(res_right.success)

        # Middle click
        res_mid = self.desktop_ctrl.click_mouse(100, 100, button="middle", double=False)
        self.assertTrue(res_mid.success)

        # Invalid button validation
        res_invalid_btn = self.desktop_ctrl.click_mouse(100, 100, button="invalid_button")
        self.assertFalse(res_invalid_btn.success)
        self.assertEqual(res_invalid_btn.error, "INVALID_BUTTON")

    @patch("ctypes.windll.user32.mouse_event")
    def test_mouse_scroll_and_validation(self, mock_mouse_event):
        # Valid down scroll
        res_down = self.desktop_ctrl.scroll_mouse(direction="down", amount=3)
        self.assertTrue(res_down.success)
        self.assertTrue(res_down.verified)

        # Valid up scroll
        res_up = self.desktop_ctrl.scroll_mouse(direction="up", amount=2)
        self.assertTrue(res_up.success)

        # Invalid direction
        res_inv_dir = self.desktop_ctrl.scroll_mouse(direction="sideways", amount=3)
        self.assertFalse(res_inv_dir.success)
        self.assertEqual(res_inv_dir.error, "INVALID_DIRECTION")

        # Out-of-bounds amount (> 20 or < 1)
        res_too_high = self.desktop_ctrl.scroll_mouse(direction="down", amount=100)
        self.assertFalse(res_too_high.success)
        self.assertEqual(res_too_high.error, "OUT_OF_BOUNDS")

        res_too_low = self.desktop_ctrl.scroll_mouse(direction="down", amount=0)
        self.assertFalse(res_too_low.success)
        self.assertEqual(res_too_low.error, "OUT_OF_BOUNDS")

    @patch("ctypes.windll.user32.keybd_event")
    @patch("ctypes.windll.user32.VkKeyScanW", return_value=0x41)
    def test_type_text(self, mock_vk, mock_keybd):
        res = self.desktop_ctrl.type_text("Hello")
        self.assertTrue(res.success)
        self.assertTrue(res.verified)
        self.assertEqual(res.data["length"], 5)
        self.assertTrue(mock_keybd.called)

        # Empty text
        res_empty = self.desktop_ctrl.type_text("")
        self.assertFalse(res_empty.success)
        self.assertEqual(res_empty.error, "EMPTY_TEXT")

        # Exceeds limit (> 500 chars)
        long_text = "a" * 501
        res_long = self.desktop_ctrl.type_text(long_text)
        self.assertFalse(res_long.success)
        self.assertEqual(res_long.error, "TEXT_TOO_LONG")

    @patch("ctypes.windll.user32.keybd_event")
    def test_press_key(self, mock_keybd):
        res_enter = self.desktop_ctrl.press_key("enter")
        self.assertTrue(res_enter.success)
        self.assertTrue(res_enter.verified)

        res_esc = self.desktop_ctrl.press_key("esc")
        self.assertTrue(res_esc.success)

        # Unsupported key
        res_invalid = self.desktop_ctrl.press_key("invalid_key_xyz")
        self.assertFalse(res_invalid.success)
        self.assertEqual(res_invalid.error, "UNSUPPORTED_KEY")

    @patch("ctypes.windll.user32.keybd_event")
    @patch("ctypes.windll.user32.VkKeyScanW", return_value=0x41)
    def test_hotkey(self, mock_vk, mock_keybd):
        res_hotkey = self.desktop_ctrl.hotkey("ctrl", "a")
        self.assertTrue(res_hotkey.success)
        self.assertTrue(res_hotkey.verified)

        res_empty = self.desktop_ctrl.hotkey()
        self.assertFalse(res_empty.success)
        self.assertEqual(res_empty.error, "EMPTY_KEYS")

        res_invalid = self.desktop_ctrl.hotkey("ctrl", "nonexistent_key_123")
        self.assertFalse(res_invalid.success)
        self.assertEqual(res_invalid.error, "UNSUPPORTED_KEY")

    @patch("PIL.ImageGrab.grab")
    def test_capture_and_describe_screen(self, mock_grab):
        fake_img = Image.new("RGB", (1920, 1080), color="blue")
        mock_grab.return_value = fake_img

        res_cap = self.desktop_ctrl.capture_screen(save=True)
        self.assertTrue(res_cap.success)
        self.assertTrue(res_cap.verified)
        self.assertIsNotNone(res_cap.data.get("path"))

        res_desc = self.desktop_ctrl.describe_screen()
        self.assertTrue(res_desc.success)
        self.assertTrue(res_desc.verified)
        self.assertIn("1920x1080 resolution", res_desc.message)
        self.assertIn("landscape", res_desc.message)
        self.assertIn("Semantic visual interpretation is not yet implemented", res_desc.message)

    # -------------------------------------------------------------
    # 2. Browser Advanced Inspection Unit Tests
    # -------------------------------------------------------------

    def test_browser_page_content_and_links(self):
        # Test inspection on a real controlled Playwright page using data URL (no external network required)
        test_html = "data:text/html,%3Ch1%3ETest%20Page%3C/h1%3E%3Cp%3EWelcome%20to%20FRIDAY%3C/p%3E%3Ca%20href=%22https://example.com/learn%22%3ELearn%20More%3C/a%3E"
        res_open = self.browser_ctrl.open_website(test_html)
        self.assertTrue(res_open.success)

        res_content = self.browser_ctrl.get_page_content()
        self.assertTrue(res_content.success)
        self.assertIn("Test Page Welcome to FRIDAY Learn More", res_content.message)

        res_links = self.browser_ctrl.get_visible_links()
        self.assertTrue(res_links.success)
        self.assertEqual(len(res_links.data["links"]), 1)
        self.assertEqual(res_links.data["links"][0][0], "Learn More")
        self.assertIn("https://example.com/learn", res_links.data["links"][0][1])

    def test_browser_inspection_without_session(self):
        res_content = self.browser_ctrl.get_page_content()
        self.assertFalse(res_content.success)
        self.assertEqual(res_content.error, "NO_ACTIVE_SESSION")

        res_links = self.browser_ctrl.get_visible_links()
        self.assertFalse(res_links.success)
        self.assertEqual(res_links.error, "NO_ACTIVE_SESSION")

    # -------------------------------------------------------------
    # 3. Approval Requirement Policy Gating Tests
    # -------------------------------------------------------------

    def test_consequential_computer_control_requires_approval(self):
        executor = PlanExecutor()
        consequential_actions = [
            ("click_mouse", {"x": 100, "y": 100}),
            ("type_text", {"text": "Hello"}),
            ("press_key", {"key": "enter"}),
            ("hotkey", {"keys": ["ctrl", "a"]}),
        ]
        for tool, args in consequential_actions:
            action = ProposedAction(
                tool=tool,
                arguments=args,
                requires_approval=False,
                reason="test",
            )
            # Default allow_approved=False should block execution
            res = executor.execute_action(action, allow_approved=False)
            self.assertFalse(res.executed)
            self.assertEqual(res.error, "APPROVAL_REQUIRED")
            self.assertTrue(res.requires_approval)

    # -------------------------------------------------------------
    # 4. Orchestrator Integration Tests
    # -------------------------------------------------------------

    @patch("PIL.ImageGrab.grab")
    def test_orchestrator_capture_screen(self, mock_grab):
        mock_grab.return_value = Image.new("RGB", (1920, 1080), color="red")
        res = self.orchestrator.handle("Take a screenshot")
        self.assertEqual(res.plan.actions[0].tool, "capture_screen")
        self.assertTrue(res.execution_result.results[0].success)

    @patch("PIL.ImageGrab.grab")
    def test_orchestrator_describe_screen(self, mock_grab):
        mock_grab.return_value = Image.new("RGB", (1920, 1080), color="green")
        res = self.orchestrator.handle("What is on my screen?")
        self.assertEqual(res.plan.actions[0].tool, "describe_screen")
        self.assertTrue(res.execution_result.results[0].success)
        self.assertIn("Screen captured successfully", res.message)

    @patch("tools.desktop.DesktopController.move_mouse")
    def test_orchestrator_move_mouse(self, mock_move):
        from tools.desktop import DesktopActionResult
        mock_move.return_value = DesktopActionResult(success=True, message="Moved mouse", action="move_mouse", verified=True)
        res = self.orchestrator.handle("Move mouse to 500, 400")
        self.assertEqual(res.plan.actions[0].tool, "move_mouse")
        self.assertEqual(res.plan.actions[0].arguments, {"x": 500, "y": 400})
        self.assertTrue(res.execution_result.results[0].success)

    @patch("tools.desktop.DesktopController.scroll_mouse")
    def test_orchestrator_scroll_mouse(self, mock_scroll):
        from tools.desktop import DesktopActionResult
        mock_scroll.return_value = DesktopActionResult(success=True, message="Scrolled mouse", action="scroll_mouse", verified=True)
        res = self.orchestrator.handle("Scroll down")
        self.assertEqual(res.plan.actions[0].tool, "scroll_mouse")
        self.assertTrue(res.execution_result.results[0].success)

    def test_orchestrator_browser_content_and_links(self):
        test_html = "data:text/html,%3Ch1%3EWelcome%3C/h1%3E%3Ca%20href=%22https://example.com/docs%22%3EDocumentation%3C/a%3E"
        self.orchestrator.handle(f"Open {test_html}")
        res_content = self.orchestrator.handle("What content is visible?")
        self.assertEqual(res_content.plan.actions[0].tool, "browser_get_page_content")
        self.assertTrue(res_content.execution_result.results[0].success)
        self.assertIn("Welcome", res_content.message)

        res_links = self.orchestrator.handle("What links are visible?")
        self.assertEqual(res_links.plan.actions[0].tool, "browser_get_visible_links")
        self.assertTrue(res_links.execution_result.results[0].success)
        self.assertIn("Documentation", res_links.message)

    @patch("PIL.ImageGrab.grab")
    @patch("tools.desktop.DesktopController.move_mouse")
    def test_orchestrator_multi_step_computer_control(self, mock_move, mock_grab):
        from tools.desktop import DesktopActionResult
        mock_grab.return_value = Image.new("RGB", (1920, 1080), color="black")
        mock_move.return_value = DesktopActionResult(success=True, message="Moved mouse", action="move_mouse", verified=True)

    # -------------------------------------------------------------
    # 5. Playwright Integration Tests (Real Browser Lifecycle)
    # -------------------------------------------------------------

    def test_playwright_full_lifecycle_and_verification(self):
        # 1. Verify initialization
        ready, err = self.browser_ctrl._ensure_browser()
        self.assertTrue(ready)
        self.assertIsNone(err)
        self.assertTrue(self.browser_ctrl.is_active)

        # 2. Open controlled local page
        page1_html = "data:text/html,%3Chtml%3E%3Chead%3E%3Ctitle%3EPage%20One%3C/title%3E%3C/head%3E%3Cbody%3E%3Ch1%3EFirst%20Heading%3C/h1%3E%3Ca%20href=%22https://example.com/one%22%3ELink%20One%3C/a%3E%3C/body%3E%3C/html%3E"
        res_open = self.browser_ctrl.open_website(page1_html)
        self.assertTrue(res_open.success)
        self.assertIn("Page One", self.browser_ctrl.get_page_title().title)

        # 3. Verify DOM text content and links
        res_content = self.browser_ctrl.get_page_content()
        self.assertTrue(res_content.success)
        self.assertIn("First Heading", res_content.message)

        res_links = self.browser_ctrl.get_visible_links()
        self.assertTrue(res_links.success)
        self.assertEqual(len(res_links.data["links"]), 1)
        self.assertEqual(res_links.data["links"][0][0], "Link One")

        # 4. Open second page to test back/forward/refresh
        page2_html = "data:text/html,%3Chtml%3E%3Chead%3E%3Ctitle%3EPage%20Two%3C/title%3E%3C/head%3E%3Cbody%3E%3Ch2%3ESecond%20Heading%3C/h2%3E%3C/body%3E%3C/html%3E"
        self.browser_ctrl.open_website(page2_html)
        self.assertIn("Page Two", self.browser_ctrl.get_page_title().title)

        # Go back
        res_back = self.browser_ctrl.go_back()
        self.assertTrue(res_back.success)
        self.assertIn("Page One", self.browser_ctrl.get_page_title().title)

        # Go forward
        res_fwd = self.browser_ctrl.go_forward()
        self.assertTrue(res_fwd.success)
        self.assertIn("Page Two", self.browser_ctrl.get_page_title().title)

        # Refresh
        res_ref = self.browser_ctrl.refresh()
        self.assertTrue(res_ref.success)
        self.assertIn("Page Two", self.browser_ctrl.get_page_title().title)

        # 5. Reset session and verify closed-page handling
        self.browser_ctrl.reset_session()
        self.assertFalse(self.browser_ctrl.is_active)
        res_closed_content = self.browser_ctrl.get_page_content()
        self.assertFalse(res_closed_content.success)
        self.assertEqual(res_closed_content.error, "NO_ACTIVE_SESSION")


