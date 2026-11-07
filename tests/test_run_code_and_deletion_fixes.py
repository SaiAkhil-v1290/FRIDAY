import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.intent import classify_intent, extract_explicit_open_app_target
from core.orchestrator import JarvisOrchestrator
from ai.schemas import TaskPlan, ProposedAction


class TestBugFixesRunCodeAndDeletion(unittest.TestCase):
    # -------------------------------------------------------------------------
    # Bug 1: Missing Python code asks for clarification, no actions/launch
    # -------------------------------------------------------------------------
    def test_run_python_code_missing_input(self) -> None:
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("run this Python code")

        # Must NOT call the LLM planner or generate fabricated actions
        mock_llm.create_plan.assert_not_called()
        self.assertEqual(res.plan.actions, [])
        self.assertIn("please provide the python code", res.message.lower())
        self.assertIsNone(res.execution_result)

    def test_run_this_code_variants(self) -> None:
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        for phrase in ["run this code", "run the script", "execute this program"]:
            res = orchestrator.handle(phrase)
            self.assertEqual(res.plan.actions, [], f"Actions not empty for {phrase}")
            self.assertIn("provide", res.message.lower(), f"Did not ask for input on {phrase}")

    def test_open_this_file_missing_input(self) -> None:
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("open this file")
        self.assertEqual(res.plan.actions, [])
        self.assertIn("which file", res.message.lower())

    # -------------------------------------------------------------------------
    # Bug 2: File deletion request must NOT be treated as open_application
    # -------------------------------------------------------------------------
    def test_start_deleting_old_files_not_open_app(self) -> None:
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # Must not extract an app target
        self.assertIsNone(extract_explicit_open_app_target("start deleting old files"))

        res = orchestrator.handle("start deleting old files")
        # Planner not called or no actions executed
        self.assertEqual(res.plan.actions, [])
        self.assertIn("which files would you like to delete", res.message.lower())
        self.assertIsNone(res.execution_result)

    def test_similar_destructive_ambiguous_requests(self) -> None:
        mock_llm = MagicMock()
        mock_conv = MagicMock()
        mock_conv.respond.return_value = "Clarification needed."
        orchestrator = JarvisOrchestrator(llm=mock_llm, conversation_manager=mock_conv)

        phrases = [
            "start removing old files",
            "start cleaning my Downloads folder",
            "start removing unnecessary files",
            "start organizing my files",
        ]
        for phrase in phrases:
            self.assertIsNone(extract_explicit_open_app_target(phrase), f"Wrongly extracted app for: {phrase}")
            res = orchestrator.handle(phrase)
            self.assertEqual(res.plan.actions, [], f"Actions not empty for: {phrase}")
            self.assertIsNone(res.execution_result)

    # -------------------------------------------------------------------------
    # Existing valid application launching behavior remains intact
    # -------------------------------------------------------------------------
    def test_valid_application_launching_retained(self) -> None:
        # Allowlisted apps
        self.assertEqual(extract_explicit_open_app_target("open Chrome"), "Chrome")
        self.assertEqual(extract_explicit_open_app_target("open VS Code"), "VS Code")
        self.assertEqual(extract_explicit_open_app_target("launch Notepad"), "Notepad")
        self.assertEqual(extract_explicit_open_app_target("start Calculator"), "Calculator")

        # Non-allowlisted apps must still be recognized as application open requests
        self.assertEqual(extract_explicit_open_app_target("open Discord"), "Discord")
        self.assertEqual(extract_explicit_open_app_target("open Brave"), "Brave")
        self.assertEqual(extract_explicit_open_app_target("open SomeRandomUnknownApp"), "SomeRandomUnknownApp")

    def test_unknown_app_still_reaches_executor_and_fails_safely(self) -> None:
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(
            intent="open",
            message="Opening Discord",
            actions=[],
        )
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("open Discord")
        self.assertIsNotNone(res.plan)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "open_application")
        self.assertEqual(res.plan.actions[0].arguments["application"], "Discord")

        self.assertIsNotNone(res.execution_result)
        self.assertFalse(res.execution_result.results[0].success)
        self.assertEqual(res.execution_result.results[0].error, "UNKNOWN_APPLICATION")


if __name__ == "__main__":
    unittest.main()
