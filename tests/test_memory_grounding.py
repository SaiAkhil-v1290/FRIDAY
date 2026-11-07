import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# Ensure project root is in sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.database import DatabaseManager
from memory.long_term_memory import LongTermMemory
from memory.retrieval import MemoryRetriever
from memory.grounding import MemoryGrounder
from core.orchestrator import JarvisOrchestrator
from ai.schemas import TaskPlan, ProposedAction


class TestMemoryGrounding(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_grounding.db"
        self.db_manager = DatabaseManager(db_path=self.db_path)
        self.ltm = LongTermMemory(db=self.db_manager)
        self.retriever = MemoryRetriever(self.ltm)
        self.grounder = MemoryGrounder()

        self.mock_llm = MagicMock()
        self.mock_conv = MagicMock()
        self.orchestrator = JarvisOrchestrator(
            llm=self.mock_llm,
            conversation_manager=self.mock_conv,
            long_term_memory=self.ltm,
            memory_retriever=self.retriever,
            memory_grounder=self.grounder,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Known fact: Grounding detects fact query and passes exact memory context
    # -------------------------------------------------------------------------
    def test_known_fact_grounding(self) -> None:
        self.ltm.add_memory("JARVIS uses Python and runs completely locally.", category="project", importance=8)

        query = "What technology does my JARVIS project use?"
        self.assertTrue(self.grounder.is_memory_fact_query(query))

        self.mock_conv.respond.return_value = "Your JARVIS project uses Python and runs completely locally."
        res = self.orchestrator.handle(query)

        self.assertEqual(res.plan.actions, [])
        self.mock_conv.respond.assert_called_once()
        _, kwargs = self.mock_conv.respond.call_args
        mem_ctx = kwargs["memory_context"]

        self.assertIn("RETRIEVED USER MEMORY — FACTUAL REFERENCE ONLY", mem_ctx)
        self.assertIn("JARVIS uses Python and runs completely locally.", mem_ctx)
        self.assertIn("Do not invent or assume additional technologies", mem_ctx)

    # -------------------------------------------------------------------------
    # 2. Unknown fact: Grounding detects missing aspect and enforces uncertainty
    # -------------------------------------------------------------------------
    def test_unknown_fact_uncertainty_notice(self) -> None:
        self.ltm.add_memory("JARVIS uses Python and runs completely locally.", category="project", importance=8)

        query = "What database does my JARVIS project use?"
        self.assertTrue(self.grounder.is_memory_fact_query(query))

        retrieved = self.retriever.retrieve(query)
        missing = self.grounder.detect_missing_aspect(query, retrieved)
        self.assertEqual(missing, "database")

        self.mock_conv.respond.return_value = "I don't currently have a stored memory confirming which database your JARVIS project uses."
        res = self.orchestrator.handle(query)

        self.assertEqual(res.plan.actions, [])
        self.mock_conv.respond.assert_called_once()
        _, kwargs = self.mock_conv.respond.call_args
        mem_ctx = kwargs["memory_context"]

        self.assertIn("UNCERTAINTY NOTICE", mem_ctx)
        self.assertIn("stored memories do not mention any database", mem_ctx)
        self.assertIn("Do NOT guess or fabricate an answer", mem_ctx)

    # -------------------------------------------------------------------------
    # 3. Suggestions remain allowed but distinguished from facts
    # -------------------------------------------------------------------------
    def test_suggestions_allowed_with_distinction(self) -> None:
        self.ltm.add_memory("JARVIS uses Python and runs completely locally.", category="project", importance=8)

        query = "How can I improve my JARVIS project?"
        self.assertTrue(self.grounder.is_suggestion_query(query))

        self.mock_conv.respond.return_value = (
            "What I know about your project:\n- JARVIS uses Python and runs completely locally.\n\n"
            "Suggestions:\n- Add automated unit tests."
        )
        res = self.orchestrator.handle(query)

        self.mock_conv.respond.assert_called_once()
        _, kwargs = self.mock_conv.respond.call_args
        mem_ctx = kwargs["memory_context"]

        self.assertIn("SUGGESTIONS ALLOWED", mem_ctx)
        self.assertIn("Clearly distinguish known project facts", mem_ctx)
        self.assertIn("Never present recommendations as already part of the project", mem_ctx)

    # -------------------------------------------------------------------------
    # 4. No project status hallucination
    # -------------------------------------------------------------------------
    def test_anti_status_hallucination_instruction(self) -> None:
        self.ltm.add_memory("JARVIS is an assistant built with Python.", category="project", importance=7)

        query = "What do you know about my JARVIS project?"
        self.assertTrue(self.grounder.is_memory_fact_query(query))

        self.mock_conv.respond.return_value = "I know that JARVIS is an assistant built with Python."
        res = self.orchestrator.handle(query)

        _, kwargs = self.mock_conv.respond.call_args
        mem_ctx = kwargs["memory_context"]
        self.assertIn("Do not invent project status, stages, or phases", mem_ctx)

    # -------------------------------------------------------------------------
    # 5. Milestone 2C retrieval regression
    # -------------------------------------------------------------------------
    def test_milestone2c_retrieval_regression(self) -> None:
        self.ltm.add_memory("I like dark mode themes.", category="preference", importance=6)
        mems = self.retriever.retrieve("What themes do I prefer?")
        self.assertEqual(len(mems), 1)
        self.assertIn("dark mode", mems[0].content)

    # -------------------------------------------------------------------------
    # 6. Milestone 2B explicit memory command regression
    # -------------------------------------------------------------------------
    def test_milestone2b_commands_regression(self) -> None:
        # Store
        res_store = self.orchestrator.handle("Remember that I prefer Python.")
        self.assertIn("I'll remember", res_store.message)
        self.assertEqual(len(self.ltm.list_memories()), 1)

        # Recall
        res_recall = self.orchestrator.handle("What do you remember about me?")
        self.assertIn("prefer Python", res_recall.message)

        # Forget
        res_forget = self.orchestrator.handle("Forget that I prefer Python.")
        self.assertIn("forgotten", res_forget.message.lower())
        self.assertEqual(len(self.ltm.list_memories()), 0)


if __name__ == "__main__":
    unittest.main()

