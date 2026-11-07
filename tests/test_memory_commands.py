import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.database import DatabaseManager
from memory.long_term_memory import LongTermMemory
from memory.commands import MemoryCommandHandler
from core.orchestrator import JarvisOrchestrator
from ai.schemas import TaskPlan, ProposedAction


class TestMilestone2BMemoryCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_jarvis.db"
        self.db_manager = DatabaseManager(db_path=self.db_path)
        self.ltm = LongTermMemory(db=self.db_manager)
        self.handler = MemoryCommandHandler(self.ltm)

        self.mock_llm = MagicMock()
        self.mock_conv = MagicMock()
        self.orchestrator = JarvisOrchestrator(
            llm=self.mock_llm,
            conversation_manager=self.mock_conv,
            long_term_memory=self.ltm,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # Store Commands
    # -------------------------------------------------------------------------
    def test_store_memory_success(self) -> None:
        res = self.orchestrator.handle("Remember that I prefer Python.")
        self.assertEqual(res.plan.actions, [])
        self.assertIn("remember", res.message.lower())
        self.assertIn("I prefer Python.", res.message)

        # Verify saved in SQLite
        saved = self.ltm.list_memories()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].content, "I prefer Python.")
        self.assertEqual(saved[0].category, "preference")

    def test_store_memory_i_am_no_unsafe_pronoun_conversion(self) -> None:
        raw_cmd = "Remember that I am building a local AI assistant called JARVIS."
        res = self.orchestrator.handle(raw_cmd)
        self.assertEqual(res.plan.actions, [])
        self.assertNotIn("you am", res.message)
        self.assertIn('Got it — I\'ll remember: "I am building a local AI assistant called JARVIS."', res.message)

        # Verify stored content in SQLite remains completely unchanged
        saved = self.ltm.list_memories()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].content, "I am building a local AI assistant called JARVIS.")

    def test_store_duplicate_memory(self) -> None:
        self.orchestrator.handle("Remember that I prefer Python.")
        res2 = self.orchestrator.handle("Remember that I prefer Python.")
        self.assertEqual(res2.plan.actions, [])
        self.assertEqual(res2.message, "I already have that in my memory.")
        self.assertEqual(len(self.ltm.list_memories()), 1)

    def test_store_empty_content(self) -> None:
        res = self.orchestrator.handle("remember this")
        self.assertEqual(res.plan.actions, [])
        self.assertEqual(res.message, "Sure — what would you like me to remember?")
        self.assertEqual(len(self.ltm.list_memories()), 0)

    # -------------------------------------------------------------------------
    # Recall Commands
    # -------------------------------------------------------------------------
    def test_recall_empty(self) -> None:
        res = self.orchestrator.handle("What do you remember about me?")
        self.assertEqual(res.plan.actions, [])
        self.assertIn("don't have any long-term memories", res.message.lower())

    def test_recall_with_data(self) -> None:
        self.orchestrator.handle("Remember that I prefer Python.")
        self.orchestrator.handle("Save this: I am building a project called JARVIS.")

        res = self.orchestrator.handle("What do you remember about me?")
        self.assertEqual(res.plan.actions, [])
        self.assertIn("prefer Python", res.message)
        self.assertIn("project called JARVIS", res.message)

    def test_topic_recall_projects(self) -> None:
        self.orchestrator.handle("Remember that I prefer Python.")
        self.orchestrator.handle("Save this: I am building a project called JARVIS.")

        res = self.orchestrator.handle("What do you remember about my projects?")
        self.assertEqual(res.plan.actions, [])
        self.assertIn("project called JARVIS", res.message)
        self.assertNotIn("prefer Python", res.message)

    # -------------------------------------------------------------------------
    # Forget Commands
    # -------------------------------------------------------------------------
    def test_forget_specific_memory(self) -> None:
        self.orchestrator.handle("Remember that I prefer Python.")
        self.orchestrator.handle("Save this: I use Windows.")

        res = self.orchestrator.handle("Forget that I prefer Python.")
        self.assertEqual(res.plan.actions, [])
        self.assertEqual(res.message, "I've forgotten that memory.")

        remaining = self.ltm.list_memories()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].content, "I use Windows.")

    def test_forget_nonexistent_memory(self) -> None:
        res = self.orchestrator.handle("Forget that I like pineapple on pizza.")
        self.assertEqual(res.plan.actions, [])
        self.assertIn("couldn't find that memory", res.message.lower())

    def test_forget_dangerous_bulk(self) -> None:
        self.orchestrator.handle("Remember that I prefer Python.")
        res = self.orchestrator.handle("Forget everything")
        self.assertEqual(res.plan.actions, [])
        self.assertIn("destructive action", res.message.lower())

        # Verify nothing deleted
        self.assertEqual(len(self.ltm.list_memories()), 1)

    # -------------------------------------------------------------------------
    # Regression Tests for Milestone 1 Behavior
    # -------------------------------------------------------------------------
    def test_milestone1_regressions(self) -> None:
        # 1. Normal conversation
        self.mock_conv.respond.return_value = "Docker Compose is a tool for defining multi-container apps."
        r1 = self.orchestrator.handle("Explain Docker Compose.")
        self.assertEqual(r1.plan.actions, [])
        self.assertIn("Docker Compose", r1.message)

        # 2. Normal task
        self.mock_llm.create_plan.return_value = TaskPlan(
            intent="open",
            message="Opening Chrome",
            actions=[ProposedAction(tool="open_application", arguments={"application": "Chrome"}, requires_approval=False, reason="open")]
        )
        r2 = self.orchestrator.handle("open Chrome")
        self.assertEqual(len(r2.plan.actions), 1)
        self.assertEqual(r2.plan.actions[0].tool, "open_application")

        # 3. Dangerous task
        self.mock_llm.create_plan.return_value = TaskPlan(
            intent="delete",
            message="Deleting Downloads",
            actions=[ProposedAction(tool="delete_files", arguments={"folder": "Downloads", "pattern": "all"}, requires_approval=True, reason="delete")]
        )
        r3 = self.orchestrator.handle("delete all files in Downloads")
        self.assertEqual(len(r3.plan.actions), 1)
        self.assertTrue(r3.plan.actions[0].requires_approval)
        self.assertEqual(r3.plan.risk_level, "high")

        # 4. Continuation
        r4 = self.orchestrator.handle("continue the previous task")
        self.assertEqual(len(r4.plan.actions), 1)
        self.assertIn("Continuing previous task", r4.message)

        # 5. Casual conversation is NOT stored into long term memory
        self.orchestrator.handle("I ate chicken today.")
        self.orchestrator.handle("I watched a movie yesterday.")
        memories = self.ltm.list_memories()
        # Only the explicit "I prefer Python" and "I use Windows" from earlier should exist if any
        chicken_mems = [m for m in memories if "chicken" in m.content]
        self.assertEqual(len(chicken_mems), 0)


if __name__ == "__main__":
    unittest.main()

