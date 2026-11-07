import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.browser import search_browser, MAX_SEARCH_QUERY_LENGTH, BrowserSearchResult
from core.executor import PlanExecutor, ActionExecutionResult
from ai.schemas import TaskPlan, ProposedAction
from core.orchestrator import JarvisOrchestrator
from ai.intent import classify_intent, extract_explicit_search_query


class TestBrowserSearch(unittest.TestCase):
    # -------------------------------------------------------------------------
    # 1. Valid search execution & URL encoding
    # -------------------------------------------------------------------------
    @patch("webbrowser.open")
    def test_valid_search(self, mock_browser_open) -> None:
        mock_browser_open.return_value = True

        res = search_browser("Python FastAPI tutorials")
        self.assertTrue(res.success)
        self.assertEqual(res.query, "Python FastAPI tutorials")
        self.assertEqual(res.url, "https://www.google.com/search?q=Python+FastAPI+tutorials")
        mock_browser_open.assert_called_once_with("https://www.google.com/search?q=Python+FastAPI+tutorials")

    # -------------------------------------------------------------------------
    # 2. Spaces and special characters
    # -------------------------------------------------------------------------
    @patch("webbrowser.open")
    def test_special_characters_encoded(self, mock_browser_open) -> None:
        mock_browser_open.return_value = True

        res = search_browser("Python & FastAPI tutorial")
        self.assertTrue(res.success)
        self.assertEqual(res.url, "https://www.google.com/search?q=Python+%26+FastAPI+tutorial")
        mock_browser_open.assert_called_once_with("https://www.google.com/search?q=Python+%26+FastAPI+tutorial")

    # -------------------------------------------------------------------------
    # 3. Empty query
    # -------------------------------------------------------------------------
    @patch("webbrowser.open")
    def test_empty_query(self, mock_browser_open) -> None:
        res = search_browser("")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "EMPTY_QUERY")
        mock_browser_open.assert_not_called()

    # -------------------------------------------------------------------------
    # 4. Whitespace query
    # -------------------------------------------------------------------------
    @patch("webbrowser.open")
    def test_whitespace_query(self, mock_browser_open) -> None:
        res = search_browser("     ")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "EMPTY_QUERY")
        mock_browser_open.assert_not_called()

    # -------------------------------------------------------------------------
    # 5. Very long query
    # -------------------------------------------------------------------------
    @patch("webbrowser.open")
    def test_too_long_query(self, mock_browser_open) -> None:
        long_query = "a" * (MAX_SEARCH_QUERY_LENGTH + 1)
        res = search_browser(long_query)
        self.assertFalse(res.success)
        self.assertEqual(res.error, "QUERY_TOO_LONG")
        mock_browser_open.assert_not_called()

    # -------------------------------------------------------------------------
    # 6. Command injection-looking text is strictly URL-encoded data
    # -------------------------------------------------------------------------
    @patch("webbrowser.open")
    def test_command_injection_safety(self, mock_browser_open) -> None:
        mock_browser_open.return_value = True
        malicious = '"; rm -rf /"'
        res = search_browser(malicious)

        self.assertTrue(res.success)
        self.assertEqual(res.url, "https://www.google.com/search?q=%22%3B+rm+-rf+%2F%22")
        mock_browser_open.assert_called_once_with("https://www.google.com/search?q=%22%3B+rm+-rf+%2F%22")

    # -------------------------------------------------------------------------
    # 7. PlanExecutor browser_search integration
    # -------------------------------------------------------------------------
    @patch("core.executor.search_browser")
    def test_executor_browser_search(self, mock_search) -> None:
        mock_search.return_value = BrowserSearchResult(
            success=True,
            message="Successfully opened browser search for: 'Docker Compose tutorials'.",
            query="Docker Compose tutorials",
            url="https://www.google.com/search?q=Docker+Compose+tutorials",
        )
        executor = PlanExecutor()
        action = ProposedAction(
            tool="browser_search",
            arguments={"query": "Docker Compose tutorials"},
            requires_approval=False,
            reason="search",
        )

        res = executor.execute_action(action)
        self.assertTrue(res.executed)
        self.assertTrue(res.success)
        self.assertEqual(res.action_name, "browser_search")
        mock_search.assert_called_once_with("Docker Compose tutorials")

    # -------------------------------------------------------------------------
    # 8. Combined plan execution avoids duplicate browser launches (Bug 1 Fix)
    # -------------------------------------------------------------------------
    @patch("core.executor.search_browser")
    @patch("core.executor.launch_application")
    def test_combined_search_single_logical_navigation(self, mock_launch, mock_search) -> None:
        mock_search.return_value = BrowserSearchResult(
            success=True,
            message="Successfully opened browser search.",
            query="React tutorials",
        )
        mock_launch.return_value = MagicMock(
            success=True,
            message="Successfully launched Google Chrome.",
        )
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(
            intent="search",
            message="I'll search for React tutorials.",
            actions=[],
        )
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("open Chrome and search for React tutorials")

        # In Milestone 4D.2, multi-step execution handles both actions sequentially
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 2)
        self.assertEqual(res.plan.actions[0].tool, "open_application")
        self.assertEqual(res.plan.actions[1].tool, "browser_search")
        self.assertEqual(res.plan.actions[1].arguments["query"], "React tutorials")

        self.assertIsNotNone(res.execution_result)
        self.assertEqual(len(res.execution_result.results), 2)
        mock_search.assert_called_once_with("React tutorials")

    # -------------------------------------------------------------------------
    # 9. Deterministic Search Query Extraction & Special Characters (Bugs 2 & 3)
    # -------------------------------------------------------------------------
    def test_extract_explicit_search_query_preservation(self) -> None:
        # Special characters with ampersand
        q1 = extract_explicit_search_query("search for Python & FastAPI tutorial")
        self.assertEqual(q1, "Python & FastAPI tutorial")

        # Google prefix
        q2 = extract_explicit_search_query("google React useEffect tutorial")
        self.assertEqual(q2, "React useEffect tutorial")

        # Look up prefix
        q3 = extract_explicit_search_query("look up Docker Compose networking")
        self.assertEqual(q3, "Docker Compose networking")

        # C++ pointers
        q4 = extract_explicit_search_query("search for C++ pointers")
        self.assertEqual(q4, "C++ pointers")

        # Shell-like quotes and semicolons
        q5 = extract_explicit_search_query('search for "; rm -rf /"')
        self.assertEqual(q5, '"; rm -rf /"')

        # Combined open Chrome and search for ...
        q6 = extract_explicit_search_query("open Chrome and search for how to delete all files")
        self.assertEqual(q6, "how to delete all files")

    # -------------------------------------------------------------------------
    # 10. Web search priority over dangerous keywords (Bug 4 Fix)
    # -------------------------------------------------------------------------
    def test_web_search_priority_over_dangerous_keywords(self) -> None:
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(
            intent="search",
            message="Searching",
            actions=[],
        )
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # "open Chrome and search for how to delete all files"
        res1 = orchestrator.handle("open Chrome and search for how to delete all files")
        self.assertEqual(len(res1.plan.actions), 2)
        self.assertEqual(res1.plan.actions[0].tool, "open_application")
        self.assertEqual(res1.plan.actions[1].tool, "browser_search")
        self.assertEqual(res1.plan.actions[1].arguments["query"], "how to delete all files")
        self.assertFalse(res1.plan.actions[1].requires_approval)
        self.assertNotEqual(res1.plan.risk_level, "high")

        # "google format C drive meaning"
        res2 = orchestrator.handle("google format C drive meaning")
        self.assertEqual(len(res2.plan.actions), 1)
        self.assertEqual(res2.plan.actions[0].tool, "browser_search")
        self.assertEqual(res2.plan.actions[0].arguments["query"], "format C drive meaning")
        self.assertFalse(res2.plan.actions[0].requires_approval)

        # Actual local deletion MUST STILL be recognized as dangerous
        intent_del = classify_intent("delete all files in my Downloads folder")
        self.assertEqual(intent_del, "dangerous_task")

    # -------------------------------------------------------------------------
    # 11. Empty search missing input
    # -------------------------------------------------------------------------
    def test_search_for_empty_missing_input(self) -> None:
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        for phrase in ["search for", "search", "google", "look up", "open Chrome and search for"]:
            res = orchestrator.handle(phrase)
            self.assertEqual(res.plan.actions, [])
            self.assertIn("what would you like me to search for", res.message.lower())
            self.assertIsNone(res.execution_result)

    # -------------------------------------------------------------------------
    # 12. Existing Milestone 3 regressions
    # -------------------------------------------------------------------------
    def test_existing_milestone3_regressions(self) -> None:
        from tools.application import resolve_application
        self.assertIsNotNone(resolve_application("Chrome"))
        self.assertIsNotNone(resolve_application("VS Code"))
        self.assertIsNone(resolve_application("Discord"))
        self.assertIsNone(resolve_application("SomeRandomUnknownApp"))


if __name__ == "__main__":
    unittest.main()
