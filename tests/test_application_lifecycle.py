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
    close_application,
    check_application_running,
    ApplicationLaunchResult,
    ApplicationCloseResult,
    ApplicationStatusResult,
    APPLICATION_ALLOWLIST,
)
from core.executor import PlanExecutor, ActionExecutionResult, IMPLEMENTED_EXECUTION_TOOLS
from ai.schemas import TaskPlan, ProposedAction
from core.orchestrator import JarvisOrchestrator
from ai.intent import (
    classify_intent,
    extract_explicit_close_app_target,
    extract_explicit_app_status_target,
    extract_explicit_open_app_target,
    extract_explicit_search_query,
    extract_multi_step_actions,
)


class TestApplicationLifecycleControl(unittest.TestCase):
    """
    Milestone 4D.4: Safe Application Lifecycle Control Test Suite
    Tests cover:
    - Test A: Close Supported Application
    - Test B: Alias Resolution for Close & Status
    - Test C: Unknown Application Safety (No process terminated, safe rejection)
    - Test D: Application Running Status True
    - Test E: Application Running Status False
    - Test F: Search Regression (e.g. 'Search for how to close Chrome')
    - Test G: Existing Open Application Regression
    - Test H: Multi-Step Open Then Close
    - Test I: Failure Stops Subsequent Action (Stop-on-failure & skipped action presentation)
    - Test J: Arbitrary Process Safety (explorer.exe, python, svchost, system32, etc.)
    - Test K: Protected Application Policy (File Explorer not closable)
    """

    # -------------------------------------------------------------------------
    # Test A: Close Supported Application
    # -------------------------------------------------------------------------
    @patch("tools.application.terminate_process_by_pid")
    @patch("tools.application.get_running_process_names")
    def test_close_supported_application(self, mock_procs, mock_term) -> None:
        mock_procs.return_value = [(101, "chrome.exe"), (102, "chrome.exe"), (200, "powershell.exe")]
        mock_term.return_value = True

        res = close_application("Chrome")
        self.assertTrue(res.success)
        self.assertEqual(res.application, "Google Chrome")
        self.assertEqual(res.terminated_count, 2)
        # Verify terminate_process_by_pid was called for only matching chrome PIDs
        self.assertEqual(mock_term.call_count, 2)
        mock_term.assert_any_call(101)
        mock_term.assert_any_call(102)

    # -------------------------------------------------------------------------
    # Test B: Alias Resolution
    # -------------------------------------------------------------------------
    def test_alias_resolution(self) -> None:
        target_code = extract_explicit_close_app_target("Close Code")
        self.assertEqual(target_code, "Code")
        resolved = resolve_application(target_code)
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved[0], "vscode")
        self.assertEqual(resolved[1]["display_name"], "Visual Studio Code")

        target_vs = extract_explicit_close_app_target("Quit VS Code")
        self.assertEqual(target_vs, "VS Code")
        resolved_vs = resolve_application(target_vs)
        self.assertIsNotNone(resolved_vs)
        self.assertEqual(resolved_vs[0], "vscode")

        target_calc = extract_explicit_close_app_target("Exit Calculator")
        self.assertEqual(target_calc, "Calculator")
        resolved_calc = resolve_application(target_calc)
        self.assertIsNotNone(resolved_calc)
        self.assertEqual(resolved_calc[0], "calculator")

    # -------------------------------------------------------------------------
    # Test C: Unknown Application Safety
    # -------------------------------------------------------------------------
    @patch("tools.application.terminate_process_by_pid")
    @patch("tools.application.get_running_process_names")
    def test_unknown_application_safety(self, mock_procs, mock_term) -> None:
        res = close_application("SomeRandomUnknownApp")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "UNKNOWN_APPLICATION")
        self.assertIn("not in the allowed application list", res.message)
        mock_procs.assert_not_called()
        mock_term.assert_not_called()

    # -------------------------------------------------------------------------
    # Test D: Application Running Status True
    # -------------------------------------------------------------------------
    @patch("tools.application.get_running_process_names")
    def test_application_running_status_true(self, mock_procs) -> None:
        mock_procs.return_value = [(101, "chrome.exe"), (200, "python.exe")]
        res = check_application_running("Chrome")
        self.assertTrue(res.success)
        self.assertTrue(res.running)
        self.assertEqual(res.application, "Google Chrome")
        self.assertEqual(res.process_count, 1)
        self.assertEqual(res.message, "Yes, Google Chrome is currently running.")

    # -------------------------------------------------------------------------
    # Test E: Application Running Status False
    # -------------------------------------------------------------------------
    @patch("tools.application.get_running_process_names")
    def test_application_running_status_false(self, mock_procs) -> None:
        mock_procs.return_value = [(200, "python.exe"), (300, "powershell.exe")]
        res = check_application_running("Chrome")
        self.assertTrue(res.success)
        self.assertFalse(res.running)
        self.assertEqual(res.application, "Google Chrome")
        self.assertEqual(res.process_count, 0)
        self.assertEqual(res.message, "No, Google Chrome is not currently running.")

    # -------------------------------------------------------------------------
    # Test F: Search Regression
    # -------------------------------------------------------------------------
    def test_search_regression(self) -> None:
        query = "Search for how to close Chrome"
        self.assertIsNone(extract_explicit_close_app_target(query))
        extracted_q = extract_explicit_search_query(query)
        self.assertEqual(extracted_q, "how to close Chrome")

        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)
        with patch("tools.browser.webbrowser.open") as mock_open:
            mock_open.return_value = True
            result = orch.handle(query)
            self.assertEqual(result.plan.intent, "browser_search")
            self.assertEqual(result.plan.actions[0].tool, "browser_search")
            self.assertEqual(result.plan.actions[0].arguments["query"], "how to close Chrome")
            mock_llm.create_plan.assert_not_called()

    # -------------------------------------------------------------------------
    # Test G: Existing Open Application Regression
    # -------------------------------------------------------------------------
    def test_existing_open_application_regression(self) -> None:
        for app_input in ["Open Chrome", "Open VS Code", "Open Calculator"]:
            target = extract_explicit_open_app_target(app_input)
            self.assertIsNotNone(target)
            resolved = resolve_application(target)
            self.assertIsNotNone(resolved)

        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)
        with patch("core.executor.launch_application") as mock_launch:
            mock_launch.return_value = ApplicationLaunchResult(
                success=True,
                message="Successfully launched Calculator.",
                application="Calculator"
            )
            res = orch.handle("Open Calculator")
            self.assertEqual(res.plan.intent, "open_application")
            self.assertEqual(res.plan.actions[0].tool, "open_application")
            self.assertEqual(res.plan.actions[0].arguments["application"], "Calculator")
            mock_llm.create_plan.assert_not_called()

    # -------------------------------------------------------------------------
    # Test H: Multi-Step Open Then Close
    # -------------------------------------------------------------------------
    def test_multi_step_open_then_close(self) -> None:
        multi = extract_multi_step_actions("Open Chrome and then close Chrome")
        self.assertIsNotNone(multi)
        self.assertEqual(len(multi), 2)
        self.assertEqual(multi[0]["tool"], "open_application")
        self.assertEqual(multi[1]["tool"], "close_application")
        self.assertEqual(multi[0]["arguments"]["application"], "Google Chrome")
        self.assertEqual(multi[1]["arguments"]["application"], "Google Chrome")

        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)
        with patch("core.executor.launch_application") as mock_launch, patch("core.executor.close_application") as mock_close:
            mock_launch.return_value = ApplicationLaunchResult(
                success=True,
                message="Successfully launched Google Chrome.",
                application="Google Chrome"
            )
            mock_close.return_value = ApplicationCloseResult(
                success=True,
                message="Successfully closed Google Chrome.",
                application="Google Chrome",
                terminated_count=1
            )
            res = orch.handle("Open Chrome and then close Chrome")
            self.assertEqual(len(res.execution_result.results), 2)
            self.assertTrue(res.execution_result.all_successful)
            mock_launch.assert_called_once_with("Google Chrome")
            mock_close.assert_called_once_with("Google Chrome")

    # -------------------------------------------------------------------------
    # Test I: Failure Stops Subsequent Action
    # -------------------------------------------------------------------------
    def test_failure_stops_subsequent_action(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)
        with patch("core.executor.launch_application") as mock_launch, patch("core.executor.close_application") as mock_close:
            mock_close.return_value = ApplicationCloseResult(
                success=False,
                message="'SomeRandomUnknownApp' is not in the allowed application list.",
                application="SomeRandomUnknownApp",
                error="UNKNOWN_APPLICATION"
            )
            res = orch.handle("Close SomeRandomUnknownApp and then open Calculator")
            self.assertEqual(len(res.execution_result.results), 2)
            r1, r2 = res.execution_result.results
            # Action 1 failed safely
            self.assertTrue(r1.executed)
            self.assertFalse(r1.success)
            self.assertEqual(r1.error, "UNKNOWN_APPLICATION")
            # Action 2 skipped
            self.assertFalse(r2.executed)
            self.assertFalse(r2.success)
            self.assertEqual(r2.error, "PREVIOUS_ACTION_FAILED")
            self.assertEqual(r2.message, "Action was not executed because the previous action failed.")
            mock_launch.assert_not_called()

    # -------------------------------------------------------------------------
    # Test J: Arbitrary Process Safety
    # -------------------------------------------------------------------------
    @patch("tools.application.terminate_process_by_pid")
    @patch("tools.application.get_running_process_names")
    def test_arbitrary_process_safety(self, mock_procs, mock_term) -> None:
        arbitrary_targets = [
            "explorer.exe",
            "taskmgr",
            "python",
            "System",
            "svchost",
            r"C:\Windows\System32\something.exe",
            "cmd.exe",
            "powershell.exe",
        ]
        for target in arbitrary_targets:
            res = close_application(target)
            self.assertFalse(res.success, f"Target '{target}' must not succeed")
            self.assertIn(res.error, {"UNKNOWN_APPLICATION", "APPLICATION_NOT_CLOSABLE"})
            mock_term.assert_not_called()
            mock_procs.assert_not_called()

    # -------------------------------------------------------------------------
    # Test K: Protected Application Policy
    # -------------------------------------------------------------------------
    @patch("tools.application.terminate_process_by_pid")
    @patch("tools.application.get_running_process_names")
    def test_protected_application_policy(self, mock_procs, mock_term) -> None:
        # File Explorer is launchable but protected from closing
        res = close_application("File Explorer")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "APPLICATION_NOT_CLOSABLE")
        self.assertEqual(res.message, "JARVIS is not allowed to close File Explorer.")
        mock_term.assert_not_called()
        mock_procs.assert_not_called()

    # -------------------------------------------------------------------------
    # Test L: Application Not Running Handled Gracefully
    # -------------------------------------------------------------------------
    @patch("tools.application.terminate_process_by_pid")
    @patch("tools.application.get_running_process_names")
    def test_application_not_running_close_handled_gracefully(self, mock_procs, mock_term) -> None:
        mock_procs.return_value = [(200, "python.exe")]
        res = close_application("Chrome")
        self.assertTrue(res.success)
        self.assertEqual(res.terminated_count, 0)
        self.assertEqual(res.message, "Google Chrome is not currently running.")
        mock_term.assert_not_called()


if __name__ == "__main__":
    unittest.main()

