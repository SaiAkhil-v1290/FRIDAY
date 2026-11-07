import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.application import (
    normalize_app_name,
    resolve_application,
    launch_application,
    APPLICATION_ALLOWLIST,
)
from core.executor import PlanExecutor, ActionExecutionResult, IMPLEMENTED_EXECUTION_TOOLS
from ai.schemas import TaskPlan, ProposedAction


class TestSafeApplicationExecution(unittest.TestCase):
    # -------------------------------------------------------------------------
    # 1. Normalization & Allowlist Resolution
    # -------------------------------------------------------------------------
    def test_app_name_normalization(self) -> None:
        self.assertEqual(normalize_app_name("  Chrome "), "chrome")
        self.assertEqual(normalize_app_name('"Google Chrome"'), "google chrome")
        self.assertEqual(normalize_app_name("'VS Code'"), "vs code")
        self.assertEqual(normalize_app_name("Visual  Studio   Code"), "visual studio code")
        self.assertEqual(normalize_app_name(""), "")

    def test_app_allowlist_resolution_known(self) -> None:
        # Chrome aliases
        c1 = resolve_application("Chrome")
        self.assertIsNotNone(c1)
        self.assertEqual(c1[0], "chrome")

        c2 = resolve_application("Google Chrome")
        self.assertIsNotNone(c2)
        self.assertEqual(c2[0], "chrome")

        # VS Code aliases
        v1 = resolve_application("VS Code")
        self.assertIsNotNone(v1)
        self.assertEqual(v1[0], "vscode")

        v2 = resolve_application("visual studio code")
        self.assertIsNotNone(v2)
        self.assertEqual(v2[0], "vscode")

        # Notepad
        n = resolve_application("Notepad")
        self.assertIsNotNone(n)
        self.assertEqual(n[0], "notepad")

    def test_app_allowlist_resolution_unknown(self) -> None:
        self.assertIsNone(resolve_application("SomeRandomUnknownApp"))
        self.assertIsNone(resolve_application("malicious_script.exe"))
        self.assertIsNone(resolve_application("calc && rm -rf /"))

    # -------------------------------------------------------------------------
    # 2. Safe Launching Behavior
    # -------------------------------------------------------------------------
    def test_unknown_app_rejected_without_execution(self) -> None:
        with patch("subprocess.Popen") as mock_popen:
            res = launch_application("SomeRandomUnknownApp")
            self.assertFalse(res.success)
            self.assertEqual(res.error, "UNKNOWN_APPLICATION")
            mock_popen.assert_not_called()

    @patch("shutil.which")
    @patch("pathlib.Path.is_file")
    @patch("subprocess.Popen")
    def test_allowed_app_launches_without_shell(self, mock_popen, mock_is_file, mock_which) -> None:
        mock_is_file.return_value = True
        mock_which.return_value = r"C:\Windows\System32\notepad.exe"

        res = launch_application("Notepad")
        self.assertTrue(res.success)
        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        self.assertFalse(kwargs.get("shell", True), "Must NOT use shell=True")
        self.assertIn("notepad", args[0][0].lower())

    # -------------------------------------------------------------------------
    # 3. PlanExecutor Execution Tests
    # -------------------------------------------------------------------------
    def test_executor_implemented_action_executes(self) -> None:
        executor = PlanExecutor()
        plan = TaskPlan(
            intent="open",
            message="Opening Notepad",
            actions=[
                ProposedAction(
                    tool="open_application",
                    arguments={"application": "Notepad"},
                    requires_approval=False,
                    reason="open",
                )
            ],
        )

        with patch("tools.application.launch_application") as mock_launch:
            from tools.application import ApplicationLaunchResult
            mock_launch.return_value = ApplicationLaunchResult(
                success=True,
                message="Successfully launched Notepad.",
                application="Notepad",
                resolved_target=r"C:\Windows\notepad.exe",
            )

            res = executor.execute_plan(plan)
            self.assertEqual(len(res.results), 1)
            self.assertTrue(res.results[0].executed)
            self.assertTrue(res.results[0].success)
            self.assertIn("Successfully launched", res.results[0].message)

    def test_executor_unregistered_tool_rejected(self) -> None:
        executor = PlanExecutor()
        action = ProposedAction(
            tool="unregistered_tool_xyz",
            arguments={},
            requires_approval=False,
            reason="hack",
        )
        res = executor.execute_action(action)
        self.assertFalse(res.executed)
        self.assertEqual(res.error, "UNREGISTERED_TOOL")

    def test_executor_unimplemented_tool_not_executed(self) -> None:
        executor = PlanExecutor()
        action = ProposedAction(
            tool="create_document",
            arguments={"path": "notes.txt", "content": "hello"},
            requires_approval=False,
            reason="create",
        )
        res = executor.execute_action(action)
        self.assertFalse(res.executed)
        self.assertEqual(res.error, "EXECUTION_NOT_IMPLEMENTED")
        self.assertIn("not implemented yet", res.message)

    def test_executor_approval_required_held_without_approval(self) -> None:
        executor = PlanExecutor()
        action = ProposedAction(
            tool="delete_files",
            arguments={"folder": "Downloads"},
            requires_approval=True,
            reason="delete",
        )
        res = executor.execute_action(action, allow_approved=False)
        self.assertFalse(res.executed)
        self.assertEqual(res.error, "APPROVAL_REQUIRED")
        self.assertTrue(res.requires_approval)

    def test_executor_partial_failure_does_not_crash(self) -> None:
        executor = PlanExecutor()
        plan = TaskPlan(
            intent="mixed",
            message="Mixed plan",
            actions=[
                ProposedAction(tool="open_application", arguments={"application": "UnknownApp123"}, requires_approval=False, reason="launch"),
                ProposedAction(tool="open_application", arguments={"application": "Notepad"}, requires_approval=False, reason="launch"),
            ],
        )

        with patch("tools.application.launch_application") as mock_launch:
            from tools.application import ApplicationLaunchResult
            # First fails, second succeeds
            mock_launch.side_effect = [
                ApplicationLaunchResult(success=False, message="Unknown", application="UnknownApp123", error="UNKNOWN_APPLICATION"),
                ApplicationLaunchResult(success=True, message="Success", application="Notepad"),
            ]

            plan_res = executor.execute_plan(plan)
            self.assertEqual(len(plan_res.results), 2)
            self.assertFalse(plan_res.results[0].success)
            self.assertTrue(plan_res.results[1].success)


if __name__ == "__main__":
    unittest.main()

