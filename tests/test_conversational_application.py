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
from memory.working_memory import WorkingMemory
from core.orchestrator import JarvisOrchestrator
from ai.intent import (
    classify_intent,
    extract_conversational_application_action,
    extract_explicit_close_app_target,
    extract_explicit_app_status_target,
    extract_explicit_open_app_target,
    extract_explicit_search_query,
    extract_multi_step_actions,
    extract_conversational_file_action,
)


class TestConversationalApplicationContext(unittest.TestCase):
    """
    Milestone 4D.5: Conversational Application Context & Pronoun Resolution Test Suite
    Tests cover:
    - Test A: Conversational Status ('Open Chrome' -> 'Is it running?')
    - Test B: Conversational Close ('Open Calculator' -> 'Close it')
    - Test C: Alias Context ('Open Code' -> 'Close it' resolves to Visual Studio Code)
    - Test D: Explicit Target Overrides Context ('Open Chrome' -> 'Close Calculator' targets Calculator)
    - Test E: No Context Status ('Is it running?' -> safe clarification)
    - Test F: No Context Close ('Close it' -> safe clarification)
    - Test G: Failed Application Does Not Replace Context ('Open Chrome' -> 'Open Unknown' -> 'Is it running?' targets Chrome)
    - Test H: Search Protection ('Search for how to close Chrome' remains web search)
    - Test I: File Context Regression ('What is its name?', 'Where is it?', 'Show me where it is')
    - Test J: Multi-Step Open Then Close It ('Open Chrome and then close it')
    - Test K: Multi-Step Latest Application Resolution ('Open Chrome and then open Calculator and then close it' closes Calculator)
    - Test L: Protected Application Policy ('Open File Explorer' -> 'Close it' is rejected)
    - Test M: Failure Chain Safety ('Open SomeRandomUnknownApp and then close it' halts and does not close stale app)
    - Test N: Various Conversational Pronoun Phrases ('Close that app', 'Is the application currently running?', 'Close the app')
    """

    # -------------------------------------------------------------------------
    # Test A: Conversational Status
    # -------------------------------------------------------------------------
    def test_conversational_status(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        with patch("core.executor.launch_application") as mock_launch, patch("core.executor.check_application_running") as mock_status:
            mock_launch.return_value = ApplicationLaunchResult(
                success=True,
                message="Successfully launched Google Chrome.",
                application="Google Chrome"
            )
            mock_status.return_value = ApplicationStatusResult(
                success=True,
                application="Google Chrome",
                running=True,
                process_count=1,
                message="Yes, Google Chrome is currently running."
            )

            # 1. Open Chrome
            res_open = orch.handle("Open Chrome")
            self.assertEqual(res_open.plan.actions[0].tool, "open_application")
            self.assertEqual(orch.memory.get_last_application(), {"key": "chrome", "display_name": "Google Chrome"})

            # 2. Is it running?
            res_status = orch.handle("Is it running?")
            self.assertEqual(res_status.message, "Yes, Google Chrome is currently running.")
            self.assertEqual(res_status.plan.actions[0].tool, "check_application_running")
            self.assertEqual(res_status.plan.actions[0].arguments["application"], "Google Chrome")
            mock_status.assert_called_once_with("Google Chrome")
            mock_llm.create_plan.assert_not_called()

    # -------------------------------------------------------------------------
    # Test B: Conversational Close
    # -------------------------------------------------------------------------
    def test_conversational_close(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        with patch("core.executor.launch_application") as mock_launch, patch("core.executor.close_application") as mock_close:
            mock_launch.return_value = ApplicationLaunchResult(
                success=True,
                message="Successfully launched Calculator.",
                application="Calculator"
            )
            mock_close.return_value = ApplicationCloseResult(
                success=True,
                message="Successfully closed Calculator.",
                application="Calculator",
                terminated_count=1
            )

            # 1. Open Calculator
            orch.handle("Open Calculator")
            self.assertEqual(orch.memory.get_last_application(), {"key": "calculator", "display_name": "Calculator"})

            # 2. Close it
            res_close = orch.handle("Close it")
            self.assertEqual(res_close.plan.actions[0].tool, "close_application")
            self.assertEqual(res_close.plan.actions[0].arguments["application"], "Calculator")
            mock_close.assert_called_once_with("Calculator")
            mock_llm.create_plan.assert_not_called()

    # -------------------------------------------------------------------------
    # Test C: Alias Context
    # -------------------------------------------------------------------------
    def test_alias_context(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        with patch("core.executor.launch_application") as mock_launch, patch("core.executor.close_application") as mock_close:
            mock_launch.return_value = ApplicationLaunchResult(
                success=True,
                message="Successfully launched Visual Studio Code.",
                application="Visual Studio Code"
            )
            mock_close.return_value = ApplicationCloseResult(
                success=True,
                message="Successfully closed Visual Studio Code.",
                application="Visual Studio Code",
                terminated_count=1
            )

            # 1. Open Code (alias of vscode)
            orch.handle("Open Code")
            self.assertEqual(orch.memory.get_last_application(), {"key": "vscode", "display_name": "Visual Studio Code"})

            # 2. Close it
            res_close = orch.handle("Close it")
            self.assertEqual(res_close.plan.actions[0].arguments["application"], "Visual Studio Code")
            mock_close.assert_called_once_with("Visual Studio Code")
            mock_llm.create_plan.assert_not_called()

    # -------------------------------------------------------------------------
    # Test D: Explicit Target Overrides Context
    # -------------------------------------------------------------------------
    def test_explicit_target_overrides_context(self) -> None:
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
                message="Successfully closed Calculator.",
                application="Calculator",
                terminated_count=1
            )

            # 1. Open Chrome
            orch.handle("Open Chrome")
            self.assertEqual(orch.memory.get_last_application()["display_name"], "Google Chrome")

            # 2. Close Calculator explicitly
            res_close = orch.handle("Close Calculator")
            self.assertEqual(res_close.plan.actions[0].arguments["application"], "Calculator")
            mock_close.assert_called_once_with("Calculator")
            mock_llm.create_plan.assert_not_called()

    # -------------------------------------------------------------------------
    # Test E: No Context Status
    # -------------------------------------------------------------------------
    def test_no_context_status(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        res = orch.handle("Is it running?")
        self.assertIn("recently used application", res.message)
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        mock_llm.create_plan.assert_not_called()

    # -------------------------------------------------------------------------
    # Test F: No Context Close
    # -------------------------------------------------------------------------
    def test_no_context_close(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        res = orch.handle("Close it")
        self.assertIn("recently used application", res.message)
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        mock_llm.create_plan.assert_not_called()

    # -------------------------------------------------------------------------
    # Test G: Failed Application Does Not Replace Context
    # -------------------------------------------------------------------------
    def test_failed_application_does_not_replace_context(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        with patch("core.executor.launch_application") as mock_launch, patch("core.executor.check_application_running") as mock_status:
            mock_launch.side_effect = [
                ApplicationLaunchResult(success=True, message="Launched Chrome", application="Google Chrome"),
                ApplicationLaunchResult(success=False, message="Unknown app", application="SomeRandomUnknownApp", error="UNKNOWN_APPLICATION")
            ]
            mock_status.return_value = ApplicationStatusResult(
                success=True,
                application="Google Chrome",
                running=True,
                process_count=1,
                message="Yes, Google Chrome is currently running."
            )

            # 1. Open Chrome (succeeds)
            orch.handle("Open Chrome")
            self.assertEqual(orch.memory.get_last_application()["display_name"], "Google Chrome")

            # 2. Open UnknownApp (fails)
            res_fail = orch.handle("Open SomeRandomUnknownApp")
            self.assertFalse(res_fail.execution_result.results[0].success)
            # Memory must STILL be Google Chrome!
            self.assertEqual(orch.memory.get_last_application()["display_name"], "Google Chrome")

            # 3. Is it running? (must target Google Chrome)
            res_status = orch.handle("Is it running?")
            self.assertEqual(res_status.plan.actions[0].arguments["application"], "Google Chrome")
            mock_status.assert_called_once_with("Google Chrome")

    # -------------------------------------------------------------------------
    # Test H: Search Protection
    # -------------------------------------------------------------------------
    def test_search_protection(self) -> None:
        query = "Search for how to close Chrome"
        self.assertIsNone(extract_conversational_application_action(query))
        self.assertIsNone(extract_explicit_close_app_target(query))
        self.assertEqual(extract_explicit_search_query(query), "how to close Chrome")

        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)
        with patch("tools.browser.webbrowser.open") as mock_open:
            mock_open.return_value = True
            res = orch.handle(query)
            self.assertEqual(res.plan.actions[0].tool, "browser_search")
            self.assertEqual(res.plan.actions[0].arguments["query"], "how to close Chrome")

    # -------------------------------------------------------------------------
    # Test I: File Context Regression
    # -------------------------------------------------------------------------
    def test_file_context_regression(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)
        orch.memory.set_last_opened_file({
            "name": "report.pdf",
            "path": r"C:\Users\test\Downloads\report.pdf",
            "extension": ".pdf",
        })
        orch.memory.set_last_application({"key": "chrome", "display_name": "Google Chrome"})

        # File queries must NOT trigger conversational application actions
        self.assertIsNone(extract_conversational_application_action("What is its name?"))
        self.assertIsNone(extract_conversational_application_action("Where is it?"))

        res_name = orch.handle("What is its name?")
        self.assertIn("report.pdf", res_name.message)
        self.assertEqual(len(res_name.plan.actions), 0)

        res_where = orch.handle("Where is it?")
        self.assertIn("Downloads", res_where.message)

    # -------------------------------------------------------------------------
    # Test J: Multi-Step Open Then Close It
    # -------------------------------------------------------------------------
    def test_multi_step_open_then_close_it(self) -> None:
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

            res = orch.handle("Open Chrome and then close it")
            self.assertEqual(len(res.execution_result.results), 2)
            act1, act2 = res.plan.actions
            self.assertEqual(act1.tool, "open_application")
            self.assertEqual(act1.arguments["application"], "Google Chrome")
            self.assertEqual(act2.tool, "close_application")
            self.assertEqual(act2.arguments["application"], "Google Chrome")
            mock_launch.assert_called_once_with("Google Chrome")
            mock_close.assert_called_once_with("Google Chrome")

    # -------------------------------------------------------------------------
    # Test K: Multi-Step Latest Application Resolution
    # -------------------------------------------------------------------------
    def test_multi_step_latest_application_resolution(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        with patch("core.executor.launch_application") as mock_launch, patch("core.executor.close_application") as mock_close:
            mock_launch.side_effect = [
                ApplicationLaunchResult(success=True, message="Launched Chrome", application="Google Chrome"),
                ApplicationLaunchResult(success=True, message="Launched Calc", application="Calculator")
            ]
            mock_close.return_value = ApplicationCloseResult(
                success=True,
                message="Closed Calc",
                application="Calculator",
                terminated_count=1
            )

            res = orch.handle("Open Chrome and then open Calculator and then close it")
            self.assertEqual(len(res.plan.actions), 3)
            self.assertEqual(res.plan.actions[0].arguments["application"], "Google Chrome")
            self.assertEqual(res.plan.actions[1].arguments["application"], "Calculator")
            # Action 3 must close Calculator (the most recent application in sequence)
            self.assertEqual(res.plan.actions[2].arguments["application"], "Calculator")
            mock_close.assert_called_once_with("Calculator")

    # -------------------------------------------------------------------------
    # Test L: Protected Application Policy
    # -------------------------------------------------------------------------
    def test_protected_application_policy(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        with patch("core.executor.launch_application") as mock_launch:
            mock_launch.return_value = ApplicationLaunchResult(
                success=True,
                message="Successfully launched File Explorer.",
                application="File Explorer"
            )

            orch.handle("Open File Explorer")
            self.assertEqual(orch.memory.get_last_application()["display_name"], "File Explorer")

            res_close = orch.handle("Close it")
            self.assertFalse(res_close.execution_result.results[0].success)
            self.assertEqual(res_close.execution_result.results[0].error, "APPLICATION_NOT_CLOSABLE")
            self.assertIn("not allowed to close File Explorer", res_close.execution_result.results[0].message)

    # -------------------------------------------------------------------------
    # Test M: Failure Chain Safety
    # -------------------------------------------------------------------------
    def test_failure_chain_safety(self) -> None:
        mock_llm = MagicMock()
        orch = JarvisOrchestrator(llm=mock_llm)

        # Pre-seed memory with a valid previous app
        orch.memory.set_last_application({"key": "chrome", "display_name": "Google Chrome"})

        with patch("core.executor.launch_application") as mock_launch, patch("core.executor.close_application") as mock_close:
            mock_launch.return_value = ApplicationLaunchResult(
                success=False,
                message="'SomeRandomUnknownApp' is not in the allowed application list.",
                application="SomeRandomUnknownApp",
                error="UNKNOWN_APPLICATION"
            )

            res = orch.handle("Open SomeRandomUnknownApp and then close it")
            self.assertEqual(len(res.execution_result.results), 2)
            r1, r2 = res.execution_result.results
            self.assertTrue(r1.executed)
            self.assertFalse(r1.success)
            # Second action skipped via stop-on-failure
            self.assertFalse(r2.executed)
            self.assertEqual(r2.error, "PREVIOUS_ACTION_FAILED")
            # Close was never invoked against any application!
            mock_close.assert_not_called()

    # -------------------------------------------------------------------------
    # Test N: Conversational Pronoun Phrase Variations
    # -------------------------------------------------------------------------
    def test_conversational_pronoun_phrase_variations(self) -> None:
        phrases_close = [
            ("Close that app", "that app"),
            ("Quit that app", "that app"),
            ("Close the app", "the app"),
            ("Exit the application", "the application"),
            ("Close that application", "that application"),
            ("Quit it", "it"),
            ("Exit it", "it"),
        ]
        for phrase, expected_ref in phrases_close:
            res = extract_conversational_application_action(phrase)
            self.assertIsNotNone(res, f"Failed for {phrase}")
            self.assertEqual(res["action"], "close_application")
            self.assertEqual(res["reference"], expected_ref)

        phrases_status = [
            ("Is that app running?", "that app"),
            ("Is the app open?", "the app"),
            ("Is the application currently running?", "the application"),
            ("Is it open?", "it"),
            ("Is it active?", "it"),
        ]
        for phrase, expected_ref in phrases_status:
            res = extract_conversational_application_action(phrase)
            self.assertIsNotNone(res, f"Failed for {phrase}")
            self.assertEqual(res["action"], "check_application_running")
            self.assertEqual(res["reference"], expected_ref)


if __name__ == "__main__":
    unittest.main()

