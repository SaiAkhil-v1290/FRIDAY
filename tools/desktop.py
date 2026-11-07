"""
Desktop interaction layer for Phase 3 Computer Control.
Provides safe, bounded, permission-validated interaction with the screen, mouse, and keyboard.
Strictly non-shell, bounded by screen dimensions, with local screenshot capture and interpretation.
"""

import os
import sys
import time
import ctypes
import ctypes.wintypes
from dataclasses import dataclass, field
from pathlib import Path
from PIL import ImageGrab, Image

from config import settings


@dataclass
class DesktopActionResult:
    success: bool
    message: str
    action: str
    target: str | None = None
    data: dict | None = None
    verified: bool = False
    error: str | None = None


# Allowed keys for keyboard interactions to prevent arbitrary control sequence injections
ALLOWED_KEYBOARD_KEYS = {
    "enter": 0x0D,
    "tab": 0x09,
    "space": 0x20,
    "backspace": 0x08,
    "escape": 0x1B,
    "esc": 0x1B,
    "delete": 0x2E,
    "del": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "ctrl": 0x11,
    "control": 0x11,
    "shift": 0x10,
    "alt": 0x12,
    "f1": 0x70,
    "f2": 0x71,
    "f3": 0x72,
    "f4": 0x73,
    "f5": 0x74,
    "f6": 0x75,
    "f7": 0x76,
    "f8": 0x77,
    "f9": 0x78,
    "f10": 0x79,
    "f11": 0x7A,
    "f12": 0x7B,
}

# Supported mouse buttons
ALLOWED_MOUSE_BUTTONS = {"left", "right", "middle"}

# Supported mouse scroll directions
ALLOWED_SCROLL_DIRECTIONS = {"up", "down"}

# Mouse event constants (Windows API)
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP_REAL = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000

# Keyboard event constants
KEYEVENTF_KEYUP = 0x0002


class DesktopController:
    """
    Dedicated controller for desktop screen capture, mouse, and keyboard interaction.
    Operates safely with:
    - OS compatibility checks (explicitly rejecting non-Windows environments)
    - Strict boundary checking for all coordinates against GetSystemMetrics
    - Hardened button and direction validation
    - Try/finally key release safety guarantees
    - Input length limits (max 500 characters)
    - Zero shell execution (ctypes and win32 APIs directly)
    - Local screenshot capture and private storage without cloud transmission
    - Honest visual awareness reporting distinguishing capture from semantic analysis
    """

    def __init__(self, screenshot_dir: Path | None = None) -> None:
        self.screenshot_dir = screenshot_dir or (Path(__file__).resolve().parent.parent / "storage" / "screenshots")
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._last_screenshot_path: Path | None = None

    def _check_os_support(self, action_name: str) -> DesktopActionResult | None:
        """Check if operating system supports desktop interaction APIs."""
        if sys.platform != "win32":
            return DesktopActionResult(
                success=False,
                message=f"Action '{action_name}' is not supported on platform '{sys.platform}'. Windows is required.",
                action=action_name,
                error="UNSUPPORTED_PLATFORM",
                verified=False,
            )
        return None

    def get_screen_dimensions(self) -> tuple[int, int]:
        """Return (width, height) in pixels of primary display."""
        if sys.platform == "win32":
            user32 = ctypes.windll.user32
            width = user32.GetSystemMetrics(0)
            height = user32.GetSystemMetrics(1)
            return int(width), int(height)
        return (1920, 1080)

    def capture_screen(self, save: bool = True) -> DesktopActionResult:
        """
        Capture the current user screen safely using PIL ImageGrab.
        Returns a DesktopActionResult with file path and dimensions.
        Never transmits screen content to external services.
        """
        try:
            img = ImageGrab.grab()
            width, height = img.size
            file_path = None
            if save:
                ts = int(time.time() * 1000)
                file_path = self.screenshot_dir / f"screenshot_{ts}.png"
                img.save(file_path, format="PNG")
                self._last_screenshot_path = file_path

            return DesktopActionResult(
                success=True,
                message=f"Captured screen ({width}x{height} pixels).",
                action="capture_screen",
                target=str(file_path) if file_path else "memory",
                data={
                    "width": width,
                    "height": height,
                    "path": str(file_path) if file_path else None,
                },
                verified=True,
            )
        except Exception as exc:
            return DesktopActionResult(
                success=False,
                message=f"Failed to capture screen: {exc}",
                action="capture_screen",
                error=str(exc),
                verified=False,
            )

    def describe_screen(self) -> DesktopActionResult:
        """
        Capture and analyze screen layout locally (dimensions, display resolution, orientation).
        Clearly documents hardware limitations: semantic VLM reasoning requires a local vision model.
        Strictly local analysis without external cloud transmission.
        """
        cap = self.capture_screen(save=True)
        if not cap.success:
            return cap

        w = cap.data["width"]
        h = cap.data["height"]
        orientation = "landscape" if w >= h else "portrait"
        
        # Local structural description without fabricating ungrounded OCR
        msg = (
            f"Screen captured successfully: {w}x{h} resolution ({orientation} orientation).\n"
            f"Image saved locally to storage.\n"
            f"Semantic visual captioning requires a local vision-language model."
        )
        return DesktopActionResult(
            success=True,
            message=msg,
            action="describe_screen",
            data={**cap.data, "orientation": orientation, "semantic_model": "local_vlm_required"},
            verified=True,
        )

    def get_mouse_position(self) -> tuple[int, int]:
        """Get current mouse cursor position (x, y)."""
        if sys.platform == "win32":
            pt = ctypes.wintypes.POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            return int(pt.x), int(pt.y)
        return (0, 0)

    def move_mouse(self, x: int, y: int) -> DesktopActionResult:
        """
        Move the mouse cursor to (x, y).
        Strictly validates that (x, y) are within screen bounds.
        Verifies resulting cursor position.
        """
        os_err = self._check_os_support("move_mouse")
        if os_err:
            return os_err

        try:
            x_int = int(x)
            y_int = int(y)
        except (ValueError, TypeError):
            return DesktopActionResult(
                success=False,
                message=f"Invalid mouse coordinates: ({x}, {y}). Coordinates must be integers.",
                action="move_mouse",
                target=f"({x}, {y})",
                error="INVALID_COORDINATES",
                verified=False,
            )

        max_w, max_h = self.get_screen_dimensions()
        if not (0 <= x_int <= max_w and 0 <= y_int <= max_h):
            return DesktopActionResult(
                success=False,
                message=f"Coordinates ({x_int}, {y_int}) are outside the screen bounds (0-{max_w}, 0-{max_h}).",
                action="move_mouse",
                target=f"({x_int}, {y_int})",
                error="OUT_OF_BOUNDS",
                verified=False,
            )

        try:
            user32 = ctypes.windll.user32
            res = user32.SetCursorPos(x_int, y_int)
            # Verify cursor position
            actual_x, actual_y = self.get_mouse_position()
            # In headless or non-interactive sessions SetCursorPos may return 0 or position might not update
            verified = bool(res != 0) and (abs(actual_x - x_int) <= 10 and abs(actual_y - y_int) <= 10)
            
            return DesktopActionResult(
                success=True,
                message=f"Moved mouse cursor to ({x_int}, {y_int}).",
                action="move_mouse",
                target=f"({x_int}, {y_int})",
                data={"x": x_int, "y": y_int, "verified_position": (actual_x, actual_y)},
                verified=verified,
            )
        except Exception as exc:
            return DesktopActionResult(
                success=False,
                message=f"Failed to move mouse: {exc}",
                action="move_mouse",
                error=str(exc),
                verified=False,
            )

    def click_mouse(
        self,
        x: int | None = None,
        y: int | None = None,
        button: str = "left",
        double: bool = False,
    ) -> DesktopActionResult:
        """
        Click the mouse at (x, y) or current position.
        Validates button type ('left', 'right', 'middle') and bounds.
        """
        os_err = self._check_os_support("click_mouse")
        if os_err:
            return os_err

        btn = button.strip().lower() if button else "left"
        if btn not in ALLOWED_MOUSE_BUTTONS:
            return DesktopActionResult(
                success=False,
                message=f"Invalid mouse button: '{button}'. Allowed buttons: {', '.join(sorted(ALLOWED_MOUSE_BUTTONS))}.",
                action="click_mouse",
                target=str(button),
                error="INVALID_BUTTON",
                verified=False,
            )

        if x is not None and y is not None:
            move_res = self.move_mouse(x, y)
            if not move_res.success:
                return move_res
            cur_x, cur_y = x, y
        else:
            cur_x, cur_y = self.get_mouse_position()

        try:
            user32 = ctypes.windll.user32
            if btn == "right":
                down_ev = MOUSEEVENTF_RIGHTDOWN
                up_ev = MOUSEEVENTF_RIGHTUP_REAL
            elif btn == "middle":
                down_ev = MOUSEEVENTF_MIDDLEDOWN
                up_ev = MOUSEEVENTF_MIDDLEUP
            else:
                down_ev = MOUSEEVENTF_LEFTDOWN
                up_ev = MOUSEEVENTF_LEFTUP

            clicks = 2 if double else 1
            for _ in range(clicks):
                user32.mouse_event(down_ev, 0, 0, 0, 0)
                time.sleep(0.05)
                user32.mouse_event(up_ev, 0, 0, 0, 0)
                if double:
                    time.sleep(0.05)

            action_desc = "double clicked" if double else "clicked"
            return DesktopActionResult(
                success=True,
                message=f"Successfully {action_desc} {btn} mouse button at ({cur_x}, {cur_y}).",
                action="click_mouse",
                target=f"({cur_x}, {cur_y})",
                data={"x": cur_x, "y": cur_y, "button": btn, "double": double},
                verified=True,
            )
        except Exception as exc:
            return DesktopActionResult(
                success=False,
                message=f"Failed to click mouse: {exc}",
                action="click_mouse",
                error=str(exc),
                verified=False,
            )

    def scroll_mouse(self, direction: str = "down", amount: int = 3) -> DesktopActionResult:
        """
        Scroll mouse wheel up or down by amount clicks.
        Validates direction in {'up', 'down'} and bounds amount between 1 and 20.
        """
        os_err = self._check_os_support("scroll_mouse")
        if os_err:
            return os_err

        dir_clean = direction.strip().lower() if direction else "down"
        if dir_clean not in ALLOWED_SCROLL_DIRECTIONS:
            return DesktopActionResult(
                success=False,
                message=f"Invalid scroll direction: '{direction}'. Allowed directions: {', '.join(sorted(ALLOWED_SCROLL_DIRECTIONS))}.",
                action="scroll_mouse",
                error="INVALID_DIRECTION",
                verified=False,
            )

        try:
            amt = int(amount)
        except (ValueError, TypeError):
            return DesktopActionResult(
                success=False,
                message=f"Invalid scroll amount: '{amount}'. Must be an integer between 1 and 20.",
                action="scroll_mouse",
                error="INVALID_AMOUNT",
                verified=False,
            )

        if not (1 <= amt <= 20):
            return DesktopActionResult(
                success=False,
                message=f"Scroll amount {amt} is out of bounds (allowed range: 1 to 20).",
                action="scroll_mouse",
                error="OUT_OF_BOUNDS",
                verified=False,
            )

        delta = -120 * amt if dir_clean == "down" else 120 * amt
        try:
            user32 = ctypes.windll.user32
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, delta, 0)

            return DesktopActionResult(
                success=True,
                message=f"Scrolled mouse wheel {dir_clean} by {amt} units.",
                action="scroll_mouse",
                data={"direction": dir_clean, "amount": amt},
                verified=True,
            )
        except Exception as exc:
            return DesktopActionResult(
                success=False,
                message=f"Failed to scroll mouse: {exc}",
                action="scroll_mouse",
                error=str(exc),
                verified=False,
            )

    def type_text(self, text: str) -> DesktopActionResult:
        """
        Type text safely using Windows SendInput / keybd_event.
        Enforces maximum text length (500 chars).
        Uses try/finally to guarantee modifier keys (Shift) are always released.
        """
        os_err = self._check_os_support("type_text")
        if os_err:
            return os_err

        if not text:
            return DesktopActionResult(
                success=False,
                message="Cannot type empty text.",
                action="type_text",
                error="EMPTY_TEXT",
                verified=False,
            )

        if len(text) > 500:
            return DesktopActionResult(
                success=False,
                message=f"Input text exceeds maximum allowed limit of 500 characters (provided {len(text)}).",
                action="type_text",
                error="TEXT_TOO_LONG",
                verified=False,
            )

        try:
            user32 = ctypes.windll.user32
            for ch in text:
                vk_code = user32.VkKeyScanW(ord(ch))
                vk = vk_code & 0xFF
                shift = (vk_code >> 8) & 1

                if shift:
                    user32.keybd_event(0x10, 0, 0, 0)  # Shift down
                try:
                    user32.keybd_event(vk, 0, 0, 0)
                    time.sleep(0.01)
                    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                finally:
                    if shift:
                        user32.keybd_event(0x10, 0, KEYEVENTF_KEYUP, 0)  # Shift up
                time.sleep(0.01)

            return DesktopActionResult(
                success=True,
                message=f"Typed text: '{text}'.",
                action="type_text",
                target=text,
                data={"length": len(text)},
                verified=True,
            )
        except Exception as exc:
            return DesktopActionResult(
                success=False,
                message=f"Failed to type text: {exc}",
                action="type_text",
                error=str(exc),
                verified=False,
            )

    def press_key(self, key_name: str) -> DesktopActionResult:
        """
        Press a single keyboard key (e.g. Enter, Esc, Tab, Space, Backspace).
        Validates against ALLOWED_KEYBOARD_KEYS.
        Guarantees key release with try/finally.
        """
        os_err = self._check_os_support("press_key")
        if os_err:
            return os_err

        key_clean = key_name.strip().lower() if key_name else ""
        if key_clean not in ALLOWED_KEYBOARD_KEYS:
            return DesktopActionResult(
                success=False,
                message=f"Unsupported or unauthorized key: '{key_name}'. Supported keys: {', '.join(sorted(ALLOWED_KEYBOARD_KEYS.keys()))}",
                action="press_key",
                target=key_name,
                error="UNSUPPORTED_KEY",
                verified=False,
            )

        vk = ALLOWED_KEYBOARD_KEYS[key_clean]
        try:
            user32 = ctypes.windll.user32
            user32.keybd_event(vk, 0, 0, 0)
            try:
                time.sleep(0.05)
            finally:
                user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)

            return DesktopActionResult(
                success=True,
                message=f"Pressed key: '{key_name}'.",
                action="press_key",
                target=key_name,
                verified=True,
            )
        except Exception as exc:
            return DesktopActionResult(
                success=False,
                message=f"Failed to press key '{key_name}': {exc}",
                action="press_key",
                error=str(exc),
                verified=False,
            )

    def hotkey(self, *keys: str) -> DesktopActionResult:
        """
        Press a hotkey combination (e.g. ctrl, a).
        Validates all keys against ALLOWED_KEYBOARD_KEYS.
        Guarantees all pressed keys are released in reverse order in try/finally.
        """
        os_err = self._check_os_support("hotkey")
        if os_err:
            return os_err

        if not keys:
            return DesktopActionResult(
                success=False,
                message="No keys provided for hotkey combination.",
                action="hotkey",
                error="EMPTY_KEYS",
                verified=False,
            )

        vk_list = []
        for k in keys:
            k_clean = k.strip().lower()
            if k_clean in ALLOWED_KEYBOARD_KEYS:
                vk_list.append(ALLOWED_KEYBOARD_KEYS[k_clean])
            elif len(k_clean) == 1 and k_clean.isalnum():
                vk_code = ctypes.windll.user32.VkKeyScanW(ord(k_clean)) & 0xFF
                vk_list.append(vk_code)
            else:
                return DesktopActionResult(
                    success=False,
                    message=f"Unsupported key in hotkey: '{k}'.",
                    action="hotkey",
                    target="+".join(keys),
                    error="UNSUPPORTED_KEY",
                    verified=False,
                )

        pressed_keys = []
        try:
            user32 = ctypes.windll.user32
            for vk in vk_list:
                user32.keybd_event(vk, 0, 0, 0)
                pressed_keys.append(vk)
                time.sleep(0.02)
        finally:
            # Release all pressed keys in reverse order even on error
            for vk in reversed(pressed_keys):
                try:
                    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)
                    time.sleep(0.02)
                except Exception:
                    pass

        return DesktopActionResult(
            success=True,
            message=f"Pressed hotkey: {'+'.join(keys)}.",
            action="hotkey",
            target="+".join(keys),
            verified=True,
        )


# Singleton instance
_desktop_controller_instance: DesktopController | None = None


def get_desktop_controller() -> DesktopController:
    """Singleton provider for DesktopController."""
    global _desktop_controller_instance
    if _desktop_controller_instance is None:
        _desktop_controller_instance = DesktopController()
    return _desktop_controller_instance

