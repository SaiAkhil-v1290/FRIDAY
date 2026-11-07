import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

# Ensure project root is in sys.path when run directly
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memory.database import DatabaseManager
from memory.long_term_memory import LongTermMemory
from core.orchestrator import JarvisOrchestrator


def run_manual_test() -> None:
    print("=" * 60)
    print(" JARVIS Explicit Memory Commands (Milestone 2B) Demonstration")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_db_path = Path(temp_dir) / "demo_jarvis_2b.db"
        print(f"\n[Setup] Initializing isolated temporary SQLite DB:\n        {temp_db_path}")

        db_mgr = DatabaseManager(db_path=temp_db_path)
        ltm = LongTermMemory(db=db_mgr)
        mock_llm = MagicMock()
        mock_conv = MagicMock()

        orchestrator = JarvisOrchestrator(
            llm=mock_llm,
            conversation_manager=mock_conv,
            long_term_memory=ltm,
        )

        # 1. Remember something explicitly
        print("\n[Command 1] User: 'Remember that I prefer Python.'")
        res1 = orchestrator.handle("Remember that I prefer Python.")
        print(f"JARVIS: {res1.message}")
        print(f"Actions proposed: {res1.plan.actions}")

        # 2. Recall memories
        print("\n[Command 2] User: 'What do you remember about me?'")
        res2 = orchestrator.handle("What do you remember about me?")
        print(f"JARVIS: {res2.message}")
        print(f"Actions proposed: {res2.plan.actions}")

        # 3. Forget the memory
        print("\n[Command 3] User: 'Forget that I prefer Python.'")
        res3 = orchestrator.handle("Forget that I prefer Python.")
        print(f"JARVIS: {res3.message}")
        print(f"Actions proposed: {res3.plan.actions}")

        # 4. Recall again
        print("\n[Command 4] User: 'What do you remember about me?'")
        res4 = orchestrator.handle("What do you remember about me?")
        print(f"JARVIS: {res4.message}")
        print(f"Actions proposed: {res4.plan.actions}")

        # 5. Normal conversation check
        print("\n[Command 5] User: 'I ate chicken today.' (Casual conversation)")
        mock_conv.respond.return_value = "That sounds delicious!"
        res5 = orchestrator.handle("I ate chicken today.")
        print(f"JARVIS: {res5.message}")
        print(f"Actions proposed: {res5.plan.actions}")
        print(f"Total long-term memories in DB: {len(ltm.list_memories())} (casual chat was not saved)")

    print("\n" + "=" * 60)
    print(" Milestone 2B manual demonstration completed successfully.")
    print("=" * 60)


if __name__ == "__main__":
    run_manual_test()
