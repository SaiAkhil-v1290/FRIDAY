import tempfile
import unittest
from pathlib import Path

from memory.database import DatabaseManager
from memory.long_term_memory import LongTermMemory


class TestLongTermMemory(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_jarvis.db"
        self.db_manager = DatabaseManager(db_path=self.db_path)
        self.memory = LongTermMemory(db=self.db_manager)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # Database Initialization & Tables
    # -------------------------------------------------------------------------
    def test_database_initialization(self) -> None:
        self.assertTrue(self.db_path.exists())
        with self.db_manager.connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [row["name"] for row in cursor.fetchall()]
            self.assertIn("memories", tables)
            self.assertIn("profile", tables)
            self.assertIn("preferences", tables)

    # -------------------------------------------------------------------------
    # General Memory Operations
    # -------------------------------------------------------------------------
    def test_add_and_get_memory(self) -> None:
        mem = self.memory.add_memory("User likes dark mode", category="preference", importance=8)
        self.assertIsNotNone(mem.id)
        self.assertEqual(mem.content, "User likes dark mode")
        self.assertEqual(mem.category, "preference")
        self.assertEqual(mem.importance, 8)

        retrieved = self.memory.get_memory(mem.id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.id, mem.id)
        self.assertEqual(retrieved.content, "User likes dark mode")

    def test_exact_duplicate_prevention(self) -> None:
        mem1 = self.memory.add_memory("Project uses Python 3.12", category="project", importance=6)
        mem2 = self.memory.add_memory("Project uses Python 3.12", category="project", importance=9)
        self.assertEqual(mem1.id, mem2.id)
        self.assertEqual(len(self.memory.list_memories()), 1)

    def test_list_memories(self) -> None:
        self.memory.add_memory("Fact 1", category="fact", importance=5)
        self.memory.add_memory("Fact 2", category="fact", importance=5)
        self.memory.add_memory("Pref 1", category="preference", importance=5)

        all_mems = self.memory.list_memories()
        self.assertEqual(len(all_mems), 3)

        facts = self.memory.list_memories(category="fact")
        self.assertEqual(len(facts), 2)
        for f in facts:
            self.assertEqual(f.category, "fact")

    def test_search_memories_case_insensitive(self) -> None:
        self.memory.add_memory("Docker Compose container orchestration", category="project", importance=9)
        self.memory.add_memory("Docker containerization overview", category="fact", importance=7)
        self.memory.add_memory("Python asyncio guide", category="fact", importance=5)

        results = self.memory.search_memories("docker")
        self.assertEqual(len(results), 2)
        # Verify ordering by importance desc
        self.assertEqual(results[0].content, "Docker Compose container orchestration")

        # Test uppercase query
        results_upper = self.memory.search_memories("DOCKER")
        self.assertEqual(len(results_upper), 2)

    def test_update_memory(self) -> None:
        mem = self.memory.add_memory("Original fact", category="fact", importance=4)
        updated = self.memory.update_memory(
            memory_id=mem.id,
            content="Updated fact content",
            importance=8,
        )
        self.assertIsNotNone(updated)
        self.assertEqual(updated.content, "Updated fact content")
        self.assertEqual(updated.importance, 8)
        self.assertEqual(updated.category, "fact")

        # Non-existent ID returns None
        self.assertIsNone(self.memory.update_memory(9999, content="No id"))

    def test_delete_memory(self) -> None:
        mem = self.memory.add_memory("Temporary memory", category="other")
        self.assertTrue(self.memory.delete_memory(mem.id))
        self.assertIsNone(self.memory.get_memory(mem.id))
        self.assertFalse(self.memory.delete_memory(mem.id))
        self.assertFalse(self.memory.delete_memory(9999))

    # -------------------------------------------------------------------------
    # Validation Failures
    # -------------------------------------------------------------------------
    def test_validation_errors(self) -> None:
        with self.assertRaises(ValueError):
            self.memory.add_memory("   ", category="fact")
        with self.assertRaises(ValueError):
            self.memory.add_memory("Valid content", category="invalid_category")
        with self.assertRaises(ValueError):
            self.memory.add_memory("Valid content", category="fact", importance=0)
        with self.assertRaises(ValueError):
            self.memory.add_memory("Valid content", category="fact", importance=11)

    # -------------------------------------------------------------------------
    # Profile API
    # -------------------------------------------------------------------------
    def test_profile_operations(self) -> None:
        entry1 = self.memory.set_profile("preferred_name", "Sai")
        self.assertEqual(entry1.key, "preferred_name")
        self.assertEqual(entry1.value, "Sai")

        retrieved = self.memory.get_profile("preferred_name")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.value, "Sai")

        # Update existing
        entry2 = self.memory.set_profile("preferred_name", "Sai Akhil")
        self.assertEqual(entry2.value, "Sai Akhil")
        self.assertEqual(self.memory.get_profile("preferred_name").value, "Sai Akhil")

        # List
        self.memory.set_profile("city", "Hyderabad")
        all_profile = self.memory.list_profile()
        self.assertEqual(len(all_profile), 2)

        # Delete
        self.assertTrue(self.memory.delete_profile("city"))
        self.assertFalse(self.memory.delete_profile("city"))
        self.assertIsNone(self.memory.get_profile("city"))

    # -------------------------------------------------------------------------
    # Preferences API
    # -------------------------------------------------------------------------
    def test_preferences_operations(self) -> None:
        pref = self.memory.set_preference("editor", "theme", "dark")
        self.assertEqual(pref.category, "editor")
        self.assertEqual(pref.key, "theme")
        self.assertEqual(pref.value, "dark")

        # Update without duplicates
        pref2 = self.memory.set_preference("editor", "theme", "monokai")
        self.assertEqual(pref2.id, pref.id)
        self.assertEqual(pref2.value, "monokai")

        self.memory.set_preference("development", "preferred_language", "python")

        prefs = self.memory.list_preferences()
        self.assertEqual(len(prefs), 2)

        editor_prefs = self.memory.list_preferences(category="editor")
        self.assertEqual(len(editor_prefs), 1)
        self.assertEqual(editor_prefs[0].value, "monokai")

        self.assertTrue(self.memory.delete_preference("editor", "theme"))
        self.assertFalse(self.memory.delete_preference("editor", "theme"))
        self.assertIsNone(self.memory.get_preference("editor", "theme"))


if __name__ == "__main__":
    unittest.main()

