import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai.intent import classify_intent, extract_explicit_file_search_params
from ai.schemas import ProposedAction, TaskPlan
from core.executor import PlanExecutor
from core.orchestrator import JarvisOrchestrator
from tools.files import (
    DEFAULT_EXCLUDED_DIRS,
    MAX_RESULTS,
    SAFE_SEARCH_ROOTS,
    FileInfo,
    FileSearchResult,
    discover_files,
    resolve_safe_root,
)


class TestFileDiscovery(unittest.TestCase):
    def setUp(self):
        # Create a controlled sandbox directory structure mimicking safe roots
        self.test_dir = str(Path(tempfile.mkdtemp(prefix="jarvis_test_files_")).resolve())
        self.mock_desktop = Path(self.test_dir) / "Desktop"
        self.mock_documents = Path(self.test_dir) / "Documents"
        self.mock_downloads = Path(self.test_dir) / "Downloads"

        self.mock_desktop.mkdir(parents=True, exist_ok=True)
        self.mock_documents.mkdir(parents=True, exist_ok=True)
        self.mock_downloads.mkdir(parents=True, exist_ok=True)

        self.roots_patcher = patch.dict(
            SAFE_SEARCH_ROOTS,
            {
                "desktop": self.mock_desktop,
                "documents": self.mock_documents,
                "downloads": self.mock_downloads,
            },
        )
        self.roots_patcher.start()

        # Mock OS-level launches in tests to avoid popups and win32 COM fatal exceptions during automated test runs
        self.startfile_patcher = patch("os.startfile", create=True)
        self.mock_startfile = self.startfile_patcher.start()
        self.browser_patcher = patch("webbrowser.open", return_value=True)
        self.mock_browser_open = self.browser_patcher.start()

    def tearDown(self):
        self.browser_patcher.stop()
        self.startfile_patcher.stop()
        self.roots_patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_1_exact_filename_search(self):
        # Create exact file
        target_file = self.mock_downloads / "report.pdf"
        target_file.write_text("dummy report content")

        res = discover_files(query="report.pdf")
        self.assertTrue(res.success)
        self.assertEqual(len(res.files), 1)
        self.assertEqual(res.files[0].name, "report.pdf")
        self.assertEqual(res.files[0].path, str(target_file))

    def test_2_partial_filename_search(self):
        # Create multiple files with common substring
        (self.mock_documents / "project-report.pdf").write_text("doc 1")
        (self.mock_desktop / "project-notes.txt").write_text("doc 2")
        (self.mock_downloads / "unrelated.csv").write_text("doc 3")

        res = discover_files(query="project")
        self.assertTrue(res.success)
        names = [f.name for f in res.files]
        self.assertIn("project-report.pdf", names)
        self.assertIn("project-notes.txt", names)
        self.assertNotIn("unrelated.csv", names)
        self.assertEqual(len(res.files), 2)

    def test_3_extension_filtering(self):
        # Create mixed extensions
        (self.mock_documents / "main.py").write_text("print(1)")
        (self.mock_documents / "helper.py").write_text("print(2)")
        (self.mock_documents / "readme.txt").write_text("info")
        (self.mock_downloads / "paper.pdf").write_text("pdf data")

        # Search for .py
        res_py = discover_files(extension=".py")
        self.assertTrue(res_py.success)
        self.assertEqual(len(res_py.files), 2)
        for f in res_py.files:
            self.assertEqual(f.extension, ".py")

        # Search for pdf
        res_pdf = discover_files(extension="pdf")
        self.assertTrue(res_pdf.success)
        self.assertEqual(len(res_pdf.files), 1)
        self.assertEqual(res_pdf.files[0].name, "paper.pdf")

    def test_4_safe_directory_listing(self):
        # Create files in downloads
        (self.mock_downloads / "item1.zip").write_text("data")
        (self.mock_downloads / "item2.exe").write_text("data")
        (self.mock_documents / "doc.txt").write_text("data")

        # List only downloads
        res = discover_files(location="Downloads")
        self.assertTrue(res.success)
        names = [f.name for f in res.files]
        self.assertIn("item1.zip", names)
        self.assertIn("item2.exe", names)
        self.assertNotIn("doc.txt", names)

    def test_5_unsupported_system_location_rejection(self):
        # Attempt to search a system directory
        res = discover_files(location="C:\\Windows\\System32")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "UNSAFE_LOCATION")
        self.assertEqual(len(res.files), 0)

    def test_6_path_traversal_rejection(self):
        # Attempt traversal outside safe roots
        res = discover_files(location="../../Windows/System32")
        self.assertFalse(res.success)
        self.assertIn(res.error, ["UNSAFE_LOCATION", "INVALID_PATH"])
        self.assertEqual(len(res.files), 0)

    def test_7_shell_like_query_safety(self):
        # Malicious shell injection text
        dangerous_query = "'; rm -rf /"
        # Create a file matching this literal name
        safe_literal_name = "test_file.txt"
        (self.mock_desktop / safe_literal_name).write_text("hello")

        # Searching for dangerous query must not crash, execute shell, or delete anything
        res = discover_files(query=dangerous_query)
        self.assertTrue(res.success)
        # File remains intact
        self.assertTrue((self.mock_desktop / safe_literal_name).exists())

    def test_8_empty_search_handling(self):
        # Empty criteria should safely list all files in safe roots without crashing
        (self.mock_desktop / "a.txt").write_text("a")
        res = discover_files()
        self.assertTrue(res.success)
        self.assertEqual(len(res.files), 1)

    def test_9_result_limit(self):
        # Create 110 files in mock downloads
        for i in range(110):
            (self.mock_downloads / f"test_{i:03d}.txt").write_text("x")

        res = discover_files(location="Downloads", max_results=50)
        self.assertTrue(res.success)
        self.assertTrue(res.limited)
        self.assertEqual(len(res.files), 50)
        self.assertIn("first 50", res.message)

    def test_10_filesystem_error_resilience(self):
        # Simulate an unexpected OSError during traversal
        with patch("os.walk", side_effect=PermissionError("Access denied")):
            res = discover_files()
            self.assertTrue(res.success)
            self.assertEqual(len(res.files), 0)

    def test_11_executor_integration(self):
        # Create file in downloads
        (self.mock_downloads / "notes.txt").write_text("meeting notes")

        executor = PlanExecutor()
        plan = TaskPlan(
            intent="find_files",
            message="Finding notes",
            actions=[
                ProposedAction(
                    tool="find_files",
                    arguments={"query": "notes", "folder": "Downloads"},
                    requires_approval=False,
                    reason="User requested finding notes",
                )
            ],
        )

        exec_res = executor.execute_plan(plan)
        self.assertTrue(exec_res.all_successful)
        self.assertEqual(len(exec_res.results), 1)
        action_res = exec_res.results[0]
        self.assertTrue(action_res.executed)
        self.assertTrue(action_res.success)
        self.assertIn("notes.txt", action_res.message)

    def test_12_delete_safety_regression(self):
        # "delete all files in my Downloads folder" must remain dangerous_task, require approval, and be unimplemented
        intent = classify_intent("delete all files in my Downloads folder")
        self.assertEqual(intent, "dangerous_task")

        executor = PlanExecutor()
        del_plan = TaskPlan(
            intent="delete_files",
            message="Delete plan",
            actions=[
                ProposedAction(
                    tool="delete_files",
                    arguments={"folder": "Downloads", "pattern": "all"},
                    requires_approval=True,
                    reason="Delete files",
                )
            ],
            risk_level="high",
        )

        # Without approval: held
        res_unapproved = executor.execute_plan(del_plan, allow_approved=False)
        self.assertFalse(res_unapproved.results[0].executed)
        self.assertEqual(res_unapproved.results[0].error, "APPROVAL_REQUIRED")

        # The legacy override cannot bypass approval enforcement.
        res_override = executor.execute_plan(del_plan, allow_approved=True)
        self.assertFalse(res_override.results[0].executed)
        self.assertEqual(res_override.results[0].error, "APPROVAL_REQUIRED")

    def test_13_application_launch_regression(self):
        # Ensure open_application still works cleanly
        executor = PlanExecutor()
        app_plan = TaskPlan(
            intent="open_application",
            message="Open Notepad",
            actions=[
                ProposedAction(
                    tool="open_application",
                    arguments={"application": "notepad"},
                    requires_approval=False,
                    reason="Open Notepad",
                )
            ],
        )
        with patch("core.executor.launch_application") as mock_launch:
            mock_launch.return_value = MagicMock(success=True, message="Notepad launched", error=None)
            res = executor.execute_plan(app_plan)
            self.assertTrue(res.all_successful)
            mock_launch.assert_called_once_with("notepad")

    def test_14_browser_search_regression(self):
        # Ensure browser search with special characters & shell-like text is preserved as data
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="browser_search", message="search", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # Query with & and +
        res1 = orchestrator.handle("search for Python & FastAPI tutorial")
        self.assertEqual(res1.plan.actions[0].tool, "browser_search")
        self.assertEqual(res1.plan.actions[0].arguments["query"], "Python & FastAPI tutorial")

        # Query with shell syntax
        res2 = orchestrator.handle("search for '; rm -rf /'")
        self.assertEqual(res2.plan.actions[0].tool, "browser_search")
        self.assertEqual(res2.plan.actions[0].arguments["query"], "'; rm -rf /'")

    def test_15_orchestrator_deterministic_file_discovery(self):
        # Verify orchestrator handles various file discovery commands end-to-end
        (self.mock_documents / "my_script.py").write_text("x = 1")
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("find my Python files")
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "find_files")
        self.assertEqual(res.plan.actions[0].arguments.get("extension"), ".py")
        self.assertFalse(res.plan.actions[0].requires_approval)
        self.assertTrue(res.execution_result.all_successful)
        self.assertIn("my_script.py", res.execution_result.results[0].message)

    # -------------------------------------------------------------
    # Milestone 4B.1 Specific Tests
    # -------------------------------------------------------------

    def test_16_exact_filename_orchestrator_execution(self):
        # Test 1: "find report.pdf" generates find_files action
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("find report.pdf")
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "find_files")
        self.assertEqual(res.plan.actions[0].arguments.get("query"), "report.pdf")

    def test_17_filename_with_spaces_preserved(self):
        # Test 2: "find Homemade Indian Food Menu.png" preserves spaces
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("find Homemade Indian Food Menu.png")
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "find_files")
        self.assertEqual(res.plan.actions[0].arguments.get("query"), "Homemade Indian Food Menu.png")

    def test_18_partial_filename_with_spaces(self):
        # Test 3: "find Homemade Indian Food"
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("find Homemade Indian Food")
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "find_files")
        self.assertEqual(res.plan.actions[0].arguments.get("query"), "Homemade Indian Food")

    def test_19_filename_plus_location_without_find(self):
        # Test 4: "Homemade Indian Food Menu.png in Downloads"
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Homemade Indian Food Menu.png in Downloads")
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "find_files")
        self.assertEqual(res.plan.actions[0].arguments.get("query"), "Homemade Indian Food Menu.png")
        self.assertEqual(res.plan.actions[0].arguments.get("folder"), "Downloads")

        # More variations
        res2 = orchestrator.handle("report.pdf in Downloads")
        self.assertEqual(res2.plan.actions[0].arguments.get("query"), "report.pdf")
        self.assertEqual(res2.plan.actions[0].arguments.get("folder"), "Downloads")

        res3 = orchestrator.handle("notes.txt on Desktop")
        self.assertEqual(res3.plan.actions[0].arguments.get("query"), "notes.txt")
        self.assertEqual(res3.plan.actions[0].arguments.get("folder"), "Desktop")

    def test_20_location_only_listing(self):
        # Test 5: "show me files in Documents", "list files in Downloads", "what files are on my Desktop"
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res1 = orchestrator.handle("show me files in Documents")
        self.assertEqual(len(res1.plan.actions), 1)
        self.assertEqual(res1.plan.actions[0].tool, "find_files")
        self.assertEqual(res1.plan.actions[0].arguments.get("folder"), "Documents")
        self.assertFalse(res1.plan.actions[0].arguments.get("recursive"))
        self.assertIsNone(res1.plan.actions[0].arguments.get("query"))

        res2 = orchestrator.handle("list files in Downloads")
        self.assertEqual(res2.plan.actions[0].arguments.get("folder"), "Downloads")
        self.assertFalse(res2.plan.actions[0].arguments.get("recursive"))

        res3 = orchestrator.handle("what files are on my Desktop")
        self.assertEqual(res3.plan.actions[0].arguments.get("folder"), "Desktop")
        self.assertFalse(res3.plan.actions[0].arguments.get("recursive"))

    def test_21_excluded_virtual_environment(self):
        # Test 6: .venv/Lib/site-packages directory exclusion
        proj = self.mock_documents / "project"
        venv_lib = proj / ".venv" / "Lib" / "site-packages"
        venv_lib.mkdir(parents=True, exist_ok=True)

        (proj / "main.py").write_text("print('main')")
        (venv_lib / "package.py").write_text("print('pkg')")

        res = discover_files(extension=".py", location="Documents")
        self.assertTrue(res.success)
        names = [f.name for f in res.files]
        self.assertIn("main.py", names)
        self.assertNotIn("package.py", names)

    def test_22_excluded_node_modules(self):
        # Test 7: node_modules directory exclusion
        proj = self.mock_documents / "project"
        node_mods = proj / "node_modules" / "some-lib"
        node_mods.mkdir(parents=True, exist_ok=True)

        (proj / "app.py").write_text("print('app')")
        (node_mods / "dependency.py").write_text("print('dep')")

        res = discover_files(extension=".py", location="Documents")
        self.assertTrue(res.success)
        names = [f.name for f in res.files]
        self.assertIn("app.py", names)
        self.assertNotIn("dependency.py", names)

    def test_23_ranking_exact_matches_first(self):
        # Result ranking: exact matches appear before partial matches
        (self.mock_downloads / "report.pdf").write_text("exact")
        (self.mock_downloads / "annual-report.pdf").write_text("partial")
        (self.mock_downloads / "report-final.pdf").write_text("partial 2")

        res = discover_files(query="report.pdf", location="Downloads")
        self.assertTrue(res.success)
        self.assertGreaterEqual(len(res.files), 1)
        self.assertEqual(res.files[0].name, "report.pdf")

    # -------------------------------------------------------------
    # Milestone 4B.2 Specific Tests: Search vs Directory Listing
    # -------------------------------------------------------------

    def test_24_directory_listing_immediate_only(self):
        # Setup: Desktop has immediate files and folders, and folders have nested files
        proj_dir = self.mock_desktop / "Javy"
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "secret_code.py").write_text("code")

        nested_assets = proj_dir / "assets" / "images"
        nested_assets.mkdir(parents=True, exist_ok=True)
        (nested_assets / "nested_logo.png").write_text("img")

        (self.mock_desktop / "notes.txt").write_text("hello")
        (self.mock_desktop / "image.png").write_text("png")

        # Discover with recursive=False (Directory listing)
        res = discover_files(location="Desktop", recursive=False)
        self.assertTrue(res.success)

        dir_names = [d.name for d in res.directories]
        file_names = [f.name for f in res.files]

        # Top-level folder appears in directories
        self.assertIn("Javy", dir_names)
        self.assertNotIn("assets", dir_names)
        self.assertNotIn("images", dir_names)

        # Top-level files appear in files
        self.assertIn("notes.txt", file_names)
        self.assertIn("image.png", file_names)

        # Nested files MUST NOT appear in files or directories
        self.assertNotIn("secret_code.py", file_names)
        self.assertNotIn("nested_logo.png", file_names)

    def test_25_recursive_search_preservation(self):
        # Setup nested structure in Documents
        proj = self.mock_documents / "my_project" / "src"
        proj.mkdir(parents=True, exist_ok=True)
        (proj / "calculator.py").write_text("calc")
        (self.mock_documents / "root.py").write_text("root")

        # Search recursively
        res = discover_files(extension=".py", location="Documents", recursive=True)
        self.assertTrue(res.success)
        file_names = [f.name for f in res.files]
        self.assertIn("root.py", file_names)
        self.assertIn("calculator.py", file_names)

    def test_26_orchestrator_directory_listing_end_to_end(self):
        # Test orchestrator handling for "what files are on my Desktop"
        (self.mock_desktop / "notes.txt").write_text("notes")
        sub = self.mock_desktop / "MyFolder"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / "nested.txt").write_text("nested")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("what files are on my Desktop")
        self.assertEqual(len(res.plan.actions), 1)
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "find_files")
        self.assertEqual(action.arguments.get("folder"), "Desktop")
        self.assertFalse(action.arguments.get("recursive"))

        # Check executor output message contains separated Folders and Files
        exec_msg = res.execution_result.results[0].message
        self.assertIn("Folders", exec_msg)
        self.assertIn("MyFolder", exec_msg)
        self.assertIn("Files", exec_msg)
        self.assertIn("notes.txt", exec_msg)
        # Nested file must not be listed
        self.assertNotIn("nested.txt", exec_msg)

    def test_27_directory_listing_rejects_unsafe_location(self):
        res = discover_files(location="C:\\Windows", recursive=False)
        self.assertFalse(res.success)
        self.assertEqual(res.error, "UNSAFE_LOCATION")

    # -------------------------------------------------------------
    # Milestone 4B.3 Tests: Metadata, Filtering & Sorting
    # -------------------------------------------------------------

    def test_28_file_info_metadata_and_human_size(self):
        # 1. FileInfo contains size and modified_time
        # 2. Human-readable size format
        test_file = self.mock_documents / "test.txt"
        test_file.write_text("a" * 2048)  # 2 KB

        res = discover_files(query="test.txt", location="Documents")
        self.assertTrue(res.success)
        self.assertEqual(len(res.files), 1)
        f = res.files[0]
        self.assertEqual(f.size, 2048)
        self.assertIsNotNone(f.modified_time)
        self.assertEqual(f.human_size, "2.0 KB")
        self.assertIsNotNone(f.formatted_modified_time)

    def test_29_file_type_filtering_images_videos_documents(self):
        # Create various files
        (self.mock_downloads / "photo.png").write_text("png")
        (self.mock_downloads / "picture.jpg").write_text("jpg")
        (self.mock_downloads / "clip.mp4").write_text("mp4")
        (self.mock_downloads / "contract.docx").write_text("doc")
        (self.mock_downloads / "script.py").write_text("py")

        # Image filtering
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res_img = orchestrator.handle("find image files in Downloads")
        self.assertEqual(res_img.plan.actions[0].tool, "find_files")
        names_img = [f.name for f in discover_files(extensions=res_img.plan.actions[0].arguments["extensions"], location="Downloads").files]
        self.assertIn("photo.png", names_img)
        self.assertIn("picture.jpg", names_img)
        self.assertNotIn("clip.mp4", names_img)
        self.assertNotIn("contract.docx", names_img)

        # Video filtering
        res_vid = orchestrator.handle("find video files in Downloads")
        names_vid = [f.name for f in discover_files(extensions=res_vid.plan.actions[0].arguments["extensions"], location="Downloads").files]
        self.assertIn("clip.mp4", names_vid)
        self.assertNotIn("photo.png", names_vid)

        # Document filtering
        res_doc = orchestrator.handle("find documents in Downloads")
        names_doc = [f.name for f in discover_files(extensions=res_doc.plan.actions[0].arguments["extensions"], location="Downloads").files]
        self.assertIn("contract.docx", names_doc)
        self.assertNotIn("photo.png", names_doc)

    def test_30_sorting_newest_oldest_largest_smallest(self):
        # Create 3 files with different times and sizes
        f1 = self.mock_desktop / "small_old.txt"
        f1.write_text("1")  # 1 byte
        os.utime(f1, (1000, 1000))

        f2 = self.mock_desktop / "med_new.txt"
        f2.write_text("1" * 500)  # 500 bytes
        os.utime(f2, (5000, 5000))

        f3 = self.mock_desktop / "large_mid.txt"
        f3.write_text("1" * 2000)  # 2000 bytes
        os.utime(f3, (3000, 3000))

        # Newest (mtime desc): f2, f3, f1
        res_newest = discover_files(location="Desktop", sort_by="modified_time", sort_order="desc")
        self.assertEqual([f.name for f in res_newest.files], ["med_new.txt", "large_mid.txt", "small_old.txt"])

        # Oldest (mtime asc): f1, f3, f2
        res_oldest = discover_files(location="Desktop", sort_by="modified_time", sort_order="asc")
        self.assertEqual([f.name for f in res_oldest.files], ["small_old.txt", "large_mid.txt", "med_new.txt"])

        # Largest (size desc): f3, f2, f1
        res_largest = discover_files(location="Desktop", sort_by="size", sort_order="desc")
        self.assertEqual([f.name for f in res_largest.files], ["large_mid.txt", "med_new.txt", "small_old.txt"])

        # Smallest (size asc): f1, f2, f3
        res_smallest = discover_files(location="Desktop", sort_by="size", sort_order="asc")
        self.assertEqual([f.name for f in res_smallest.files], ["small_old.txt", "med_new.txt", "large_mid.txt"])

    def test_31_time_based_filtering(self):
        import datetime
        mock_now = datetime.datetime(2026, 11, 5, 14, 0, 0)
        # today timestamp: 2026-11-05 10:00:00
        t_today = datetime.datetime(2026, 11, 5, 10, 0, 0).timestamp()
        # yesterday timestamp: 2026-11-04 12:00:00
        t_yesterday = datetime.datetime(2026, 11, 4, 12, 0, 0).timestamp()
        # last week timestamp: 2026-10-20 12:00:00
        t_old = datetime.datetime(2026, 10, 20, 12, 0, 0).timestamp()

        f_today = self.mock_downloads / "today.txt"
        f_today.write_text("t")
        os.utime(f_today, (t_today, t_today))

        f_yest = self.mock_downloads / "yesterday.txt"
        f_yest.write_text("y")
        os.utime(f_yest, (t_yesterday, t_yesterday))

        f_old = self.mock_downloads / "old.txt"
        f_old.write_text("o")
        os.utime(f_old, (t_old, t_old))

        # Filter today
        res_today = discover_files(location="Downloads", time_filter="today", now=mock_now)
        self.assertEqual([f.name for f in res_today.files], ["today.txt"])

        # Filter yesterday
        res_yest = discover_files(location="Downloads", time_filter="yesterday", now=mock_now)
        self.assertEqual([f.name for f in res_yest.files], ["yesterday.txt"])

        # Filter this_week
        res_week = discover_files(location="Downloads", time_filter="this_week", now=mock_now)
        names_week = [f.name for f in res_week.files]
        self.assertIn("today.txt", names_week)
        self.assertIn("yesterday.txt", names_week)
        self.assertNotIn("old.txt", names_week)

    def test_32_size_based_filtering(self):
        # 1 KB and 10 MB files
        f_small = self.mock_documents / "small.dat"
        f_small.write_text("x" * 1024)  # 1 KB

        f_big = self.mock_documents / "big.dat"
        f_big.write_text("x" * (5 * 1024 * 1024))  # 5 MB

        # Larger than 2 MB
        res_gt = discover_files(location="Documents", size_filter={"operator": "gt", "bytes": 2 * 1024 * 1024})
        self.assertEqual([f.name for f in res_gt.files], ["big.dat"])

        # Smaller than 2 MB
        res_lt = discover_files(location="Documents", size_filter={"operator": "lt", "bytes": 2 * 1024 * 1024})
        self.assertEqual([f.name for f in res_lt.files], ["small.dat"])

    def test_33_combined_queries_end_to_end(self):
        # Combined: PDF in Downloads larger than 5 MB
        p1 = self.mock_downloads / "huge.pdf"
        p1.write_text("x" * (6 * 1024 * 1024))  # 6 MB

        p2 = self.mock_downloads / "tiny.pdf"
        p2.write_text("x" * 100)

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="find_files", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("find PDF files in Downloads larger than 5 MB")
        self.assertEqual(len(res.plan.actions), 1)
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "find_files")
        self.assertEqual(action.arguments.get("folder"), "Downloads")
        self.assertEqual(action.arguments.get("extension"), ".pdf")
        self.assertEqual(action.arguments.get("size_filter"), {"operator": "gt", "bytes": 5242880})

        # Verify execution result
        exec_res = res.execution_result.results[0]
        self.assertIn("huge.pdf", exec_res.message)
        self.assertNotIn("tiny.pdf", exec_res.message)
        self.assertIn("Size:", exec_res.message)


    # -------------------------------------------------------------
    # Milestone 4B.4 Tests: Safe Local File Content Search
    # -------------------------------------------------------------

    def test_34_basic_content_search(self):
        # Create a text file containing Python
        py_note = self.mock_documents / "python_guide.txt"
        py_note.write_text("JARVIS is a local assistant built in Python and runs smoothly.\nSecond line here.")

        other_note = self.mock_documents / "other.txt"
        other_note.write_text("Nothing relevant here.")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="search_file_contents", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("find files containing Python")
        self.assertEqual(len(res.plan.actions), 1)
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "search_file_contents")
        self.assertEqual(action.arguments.get("query"), "Python")

        exec_res = res.execution_result.results[0]
        self.assertTrue(exec_res.success)
        self.assertIn("python_guide.txt", exec_res.message)
        self.assertNotIn("other.txt", exec_res.message)
        self.assertIn("Line 1:", exec_res.message)

    def test_35_natural_language_extraction_botnet(self):
        # "Which file contains information about botnet detection?"
        params = extract_explicit_file_search_params("Which file contains information about botnet detection?")
        self.assertIsNotNone(params)
        self.assertEqual(params.get("mode"), "content_search")
        self.assertEqual(params.get("query"), "botnet detection")

    def test_36_mentioning_sentinelai(self):
        # "Find documents mentioning SentinelAI"
        doc_file = self.mock_documents / "ai_paper.md"
        doc_file.write_text("# Project Summary\nSentinelAI is our core security analysis engine.")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="search_file_contents", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Find documents mentioning SentinelAI")
        self.assertEqual(len(res.plan.actions), 1)
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "search_file_contents")
        self.assertEqual(action.arguments.get("query"), "SentinelAI")

        exec_res = res.execution_result.results[0]
        self.assertTrue(exec_res.success)
        self.assertIn("ai_paper.md", exec_res.message)
        self.assertIn("SentinelAI", exec_res.message)

    def test_37_explicit_inside_text_files_search(self):
        # "Search inside my text files for Python" -> must not route to browser search
        note = self.mock_downloads / "notes.txt"
        note.write_text("Learning Python programming.")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="search_file_contents", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Search inside my text files for Python")
        self.assertEqual(len(res.plan.actions), 1)
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "search_file_contents")
        self.assertEqual(action.arguments.get("query"), "Python")
        self.assertIn(".txt", action.arguments.get("extensions", []))

    def test_38_multi_word_query_docker_compose(self):
        # "Find files containing Docker Compose"
        docker_note = self.mock_desktop / "docker.md"
        docker_note.write_text("Deploy applications easily using Docker Compose in minutes.")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="search_file_contents", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Find files containing Docker Compose")
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "search_file_contents")
        self.assertEqual(action.arguments.get("query"), "Docker Compose")

        exec_res = res.execution_result.results[0]
        self.assertTrue(exec_res.success)
        self.assertIn("docker.md", exec_res.message)
        self.assertIn("Docker Compose", exec_res.message)

    def test_39_location_restriction_content_search(self):
        # File in Downloads has Python, file in Desktop has Python
        (self.mock_downloads / "in_dl.txt").write_text("Python in downloads")
        (self.mock_desktop / "in_dt.txt").write_text("Python on desktop")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="search_file_contents", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Find files in Downloads containing Python")
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "search_file_contents")
        self.assertEqual(action.arguments.get("folder"), "Downloads")

        exec_res = res.execution_result.results[0]
        self.assertTrue(exec_res.success)
        self.assertIn("in_dl.txt", exec_res.message)
        self.assertNotIn("in_dt.txt", exec_res.message)

    def test_40_unsafe_location_content_search(self):
        # "Search inside files in C:\Windows for password" -> rejected with UNSAFE_LOCATION
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="search_file_contents", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Search inside files in C:\\Windows for password")
        self.assertEqual(len(res.plan.actions), 1)
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "search_file_contents")

        exec_res = res.execution_result.results[0]
        self.assertFalse(exec_res.success)
        self.assertEqual(exec_res.error, "UNSAFE_LOCATION")

    def test_41_browser_priority_over_content_search(self):
        # "search Google for Python files" -> MUST be browser_search
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="browser_search", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("search Google for Python files")
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "browser_search")

    def test_42_binary_exclusion_content_search(self):
        # Binary file containing literal text bytes "SecretPassword123"
        bin_file = self.mock_documents / "binary_app.exe"
        bin_file.write_bytes(b"\x00\x01\x02SecretPassword123\xff\xfe")

        text_file = self.mock_documents / "safe.txt"
        text_file.write_text("SecretPassword123 in text")

        from tools.files import search_file_contents
        res = search_file_contents(query="SecretPassword123", location="Documents")
        self.assertTrue(res.success)
        matched_names = [m.name for m in res.matches]
        self.assertIn("safe.txt", matched_names)
        self.assertNotIn("binary_app.exe", matched_names)

    def test_43_snippet_output_and_line_number(self):
        # Line 5 contains the target
        sample = self.mock_documents / "code.py"
        sample.write_text("line 1\nline 2\nline 3\nline 4\nimportant_key = 'botnet detection test'\nline 6\n")

        from tools.files import search_file_contents
        res = search_file_contents(query="botnet detection", location="Documents")
        self.assertTrue(res.success)
        self.assertEqual(len(res.matches), 1)
        m = res.matches[0]
        self.assertEqual(m.name, "code.py")
        self.assertEqual(m.line_number, 5)
        self.assertIn("botnet detection", m.snippet)

    def test_44_case_insensitive_matching(self):
        sample = self.mock_documents / "case_test.txt"
        sample.write_text("PyThOn is Awesome")

        from tools.files import search_file_contents
        res = search_file_contents(query="python", location="Documents")
        self.assertTrue(res.success)
        self.assertEqual(len(res.matches), 1)
        self.assertEqual(res.matches[0].name, "case_test.txt")

    def test_45_large_file_protection(self):
        # File larger than max_file_size (default 5MB, or pass custom max_file_size)
        huge_file = self.mock_documents / "huge.txt"
        huge_file.write_text("secret" + ("0" * 2000))

        small_file = self.mock_documents / "small.txt"
        small_file.write_text("secret in small")

        from tools.files import search_file_contents
        # Use max_file_size=1000 to test skipping large file safely
        res = search_file_contents(query="secret", location="Documents", max_file_size=1000)
        self.assertTrue(res.success)
        matched_names = [m.name for m in res.matches]
        self.assertIn("small.txt", matched_names)
        self.assertNotIn("huge.txt", matched_names)

    # -------------------------------------------------------------
    # Milestone 4B.5: Safe Document Content Search Tests
    # -------------------------------------------------------------

    def test_46_search_pdf_content(self):
        # Create a PDF with pypdf
        import pypdf
        writer = pypdf.PdfWriter()
        page = writer.add_blank_page(width=72, height=72)
        # We can write text into PDF annotations or create content stream
        # Or write a simple valid PDF with text object
        # Alternatively pypdf doesn't easily create text without draw operations, but PdfWriter can add text in annotations or we can construct a stream
        # Let's verify creating a minimal valid text PDF or using ReportLab if not present:
        # A minimal PDF stream with text:
        pdf_content = (
            b"%PDF-1.4\n"
            b"1 0 obj <</Type /Catalog /Pages 2 0 R>> endobj\n"
            b"2 0 obj <</Type /Pages /Kids [3 0 R] /Count 1>> endobj\n"
            b"3 0 obj <</Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents 4 0 R /Resources <</Font <</F1 5 0 R>>>>>> endobj\n"
            b"4 0 obj <</Length 62>> stream\n"
            b"BT /F1 12 Tf 100 200 Td (Advanced Botnet Attack Detection Guide) Tj ET\n"
            b"endstream\nendobj\n"
            b"5 0 obj <</Type /Font /Subtype /Type1 /BaseFont /Helvetica>> endobj\n"
            b"xref\n0 6\n0000000000 65535 f \n0000000010 00000 n \n0000000060 00000 n \n0000000117 00000 n \n0000000242 00000 n \n0000000355 00000 n \n"
            b"trailer <</Size 6 /Root 1 0 R>>\nstartxref\n435\n%%EOF\n"
        )
        pdf_file = self.mock_downloads / "botnet_report.pdf"
        pdf_file.write_bytes(pdf_content)

        from tools.files import search_file_contents
        res = search_file_contents(query="Botnet Attack Detection", location="Downloads")
        self.assertTrue(res.success)
        self.assertEqual(len(res.matches), 1)
        m = res.matches[0]
        self.assertEqual(m.name, "botnet_report.pdf")
        self.assertEqual(m.location, "Page 1")
        self.assertIn("Botnet Attack Detection", m.snippet)

    def test_47_search_docx_content(self):
        import docx
        doc = docx.Document()
        doc.add_paragraph("First paragraph intro.")
        doc.add_paragraph("This document explains AI Maze Challenge solver in depth.")
        docx_file = self.mock_documents / "maze.docx"
        doc.save(str(docx_file))

        from tools.files import search_file_contents
        res = search_file_contents(query="AI Maze Challenge", location="Documents")
        self.assertTrue(res.success)
        self.assertEqual(len(res.matches), 1)
        m = res.matches[0]
        self.assertEqual(m.name, "maze.docx")
        self.assertEqual(m.location, "Paragraph 2")
        self.assertIn("AI Maze Challenge", m.snippet)

    def test_48_search_pptx_content(self):
        import pptx
        prs = pptx.Presentation()
        slide_layout = prs.slide_layouts[0]
        slide = prs.slides.add_slide(slide_layout)
        title = slide.shapes.title
        subtitle = slide.placeholders[1]
        title.text = "JARVIS Assistant"
        subtitle.text = "Microservices project architecture overview"
        pptx_file = self.mock_desktop / "architecture.pptx"
        prs.save(str(pptx_file))

        from tools.files import search_file_contents
        res = search_file_contents(query="project architecture", location="Desktop")
        self.assertTrue(res.success)
        self.assertEqual(len(res.matches), 1)
        m = res.matches[0]
        self.assertEqual(m.name, "architecture.pptx")
        self.assertEqual(m.location, "Slide 1")
        self.assertIn("project architecture", m.snippet)

    def test_49_search_xlsx_content(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Financials"
        ws["A1"] = "Metric"
        ws["B1"] = "Value"
        ws["A2"] = "Annual Revenue Growth"
        ws["B2"] = 1000000
        xlsx_file = self.mock_documents / "finances.xlsx"
        wb.save(str(xlsx_file))

        from tools.files import search_file_contents
        res = search_file_contents(query="Annual Revenue", location="Documents")
        self.assertTrue(res.success)
        self.assertEqual(len(res.matches), 1)
        m = res.matches[0]
        self.assertEqual(m.name, "finances.xlsx")
        self.assertEqual(m.location, 'Sheet "Financials", Cell A2')
        self.assertIn("Annual Revenue Growth", m.snippet)

    def test_50_corrupted_document_handling(self):
        # Corrupted PDF and DOCX files should not crash search_file_contents
        bad_pdf = self.mock_downloads / "corrupt.pdf"
        bad_pdf.write_bytes(b"garbage not a real pdf 123456")

        bad_docx = self.mock_documents / "corrupt.docx"
        bad_docx.write_bytes(b"PK\x03\x04corrupted zip stream")

        from tools.files import search_file_contents
        res = search_file_contents(query="test", location="Downloads")
        self.assertTrue(res.success)

    def test_51_orchestrator_natural_language_document_search(self):
        import docx
        doc = docx.Document()
        doc.add_paragraph("Section 4: Docker Compose deployment instructions.")
        doc_path = self.mock_documents / "deploy.docx"
        doc.save(str(doc_path))

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="search_file_contents", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Which Word document contains Docker Compose?")
        self.assertEqual(len(res.plan.actions), 1)
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "search_file_contents")
        self.assertEqual(action.arguments.get("query"), "Docker Compose")
        self.assertEqual(action.arguments.get("extensions"), [".docx", ".doc"])

        exec_res = res.execution_result.results[0]
        self.assertTrue(exec_res.success)
        self.assertIn("deploy.docx", exec_res.message)
        self.assertIn("Paragraph 1", exec_res.message)

    def test_52_browser_search_priority_over_document_search(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="browser_search", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Search Google for PDFs about Docker")
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "browser_search")

    def test_53_dangerous_deletion_still_requires_approval(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="dangerous_task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("delete all PDFs in Downloads")
        self.assertEqual(res.plan.risk_level, "high")
        self.assertTrue(res.plan.actions[0].requires_approval)

    # -------------------------------------------------------------
    # Milestone 4B.5.1: Bug Fix Regressions & Follow-Up Context Tests
    # -------------------------------------------------------------

    def test_54_bug1_local_presentation_search_routing(self):
        # "Search my presentations for botnet detection" must route to search_file_contents
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Search my presentations for botnet detection")
        self.assertEqual(len(res.plan.actions), 1)
        action = res.plan.actions[0]
        self.assertEqual(action.tool, "search_file_contents")
        self.assertEqual(action.arguments.get("query"), "botnet detection")
        self.assertIn(".pptx", action.arguments.get("extensions", []))

    def test_55_bug1_powerpoint_and_slides_variants(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res1 = orchestrator.handle("Search my PowerPoint files for machine learning")
        self.assertEqual(res1.plan.actions[0].tool, "search_file_contents")
        self.assertEqual(res1.plan.actions[0].arguments.get("query"), "machine learning")
        self.assertIn(".pptx", res1.plan.actions[0].arguments.get("extensions", []))

        res2 = orchestrator.handle("Find PowerPoint files mentioning Docker")
        self.assertEqual(res2.plan.actions[0].tool, "search_file_contents")
        self.assertEqual(res2.plan.actions[0].arguments.get("query"), "Docker")

        res3 = orchestrator.handle("Search my slides for neural networks")
        self.assertEqual(res3.plan.actions[0].tool, "search_file_contents")
        self.assertEqual(res3.plan.actions[0].arguments.get("query"), "neural networks")

    def test_56_bug2_local_pdf_search_routing(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # "Search my PDFs in Downloads for botnet detection"
        res1 = orchestrator.handle("Search my PDFs in Downloads for botnet detection")
        self.assertEqual(len(res1.plan.actions), 1)
        a1 = res1.plan.actions[0]
        self.assertEqual(a1.tool, "search_file_contents")
        self.assertEqual(a1.arguments.get("query"), "botnet detection")
        self.assertEqual(a1.arguments.get("folder"), "Downloads")
        self.assertEqual(a1.arguments.get("extensions"), [".pdf"])

        # "Search my PDFs for botnet detection"
        res2 = orchestrator.handle("Search my PDFs for botnet detection")
        self.assertEqual(res2.plan.actions[0].tool, "search_file_contents")
        self.assertEqual(res2.plan.actions[0].arguments.get("query"), "botnet detection")

        # "Search my PDF files for Docker"
        res3 = orchestrator.handle("Search my PDF files for Docker")
        self.assertEqual(res3.plan.actions[0].tool, "search_file_contents")
        self.assertEqual(res3.plan.actions[0].arguments.get("query"), "Docker")

        # "Search inside my PDFs for machine learning"
        res4 = orchestrator.handle("Search inside my PDFs for machine learning")
        self.assertEqual(res4.plan.actions[0].tool, "search_file_contents")
        self.assertEqual(res4.plan.actions[0].arguments.get("query"), "machine learning")

        # "Search my documents for project information"
        res5 = orchestrator.handle("Search my documents for project information")
        self.assertEqual(res5.plan.actions[0].tool, "search_file_contents")
        self.assertEqual(res5.plan.actions[0].arguments.get("query"), "project information")

    def test_57_bug3_explicit_browser_priority(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res1 = orchestrator.handle("Search Google for PDFs about Docker")
        self.assertEqual(res1.plan.actions[0].tool, "browser_search")
        self.assertEqual(res1.plan.actions[0].arguments.get("query"), "PDFs about Docker")

        res2 = orchestrator.handle("Search the web for PowerPoint presentations about AI")
        self.assertEqual(res2.plan.actions[0].tool, "browser_search")
        self.assertEqual(res2.plan.actions[0].arguments.get("query"), "PowerPoint presentations about AI")

        res3 = orchestrator.handle("Look up botnet detection online")
        self.assertEqual(res3.plan.actions[0].tool, "browser_search")
        self.assertEqual(res3.plan.actions[0].arguments.get("query"), "botnet detection")

        res4 = orchestrator.handle("Search Bing for Python documentation")
        self.assertEqual(res4.plan.actions[0].tool, "browser_search")
        self.assertEqual(res4.plan.actions[0].arguments.get("query"), "Python documentation")

    def test_58_bug4_followup_file_search_context(self):
        resume = self.mock_downloads / "Akhil_Resume(Updated).pdf"
        resume.write_text("Akhil Resume Content")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # Turn 1: "Can you find my resume in downloads"
        r1 = orchestrator.handle("Can you find my resume in downloads")
        self.assertEqual(r1.plan.actions[0].tool, "find_files")
        self.assertEqual(r1.plan.actions[0].arguments.get("folder"), "Downloads")

        # Turn 2: "Its name is Akhil_Resume(Updated) can you find it"
        r2 = orchestrator.handle("Its name is Akhil_Resume(Updated) can you find it")
        self.assertEqual(len(r2.plan.actions), 1)
        a2 = r2.plan.actions[0]
        self.assertEqual(a2.tool, "find_files")
        self.assertEqual(a2.arguments.get("query"), "Akhil_Resume(Updated)")
        self.assertEqual(a2.arguments.get("folder"), "Downloads")
        self.assertIsNotNone(r2.execution_result)
        self.assertTrue(r2.execution_result.results[0].success)
        self.assertIn("Akhil_Resume(Updated).pdf", r2.execution_result.results[0].message)

    def test_59_bug5_content_search_followup_transition(self):
        resume = self.mock_downloads / "Akhil_Resume(Updated).pdf"
        resume.write_text("Akhil Resume Content")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # Turn 1: User asks "which pdf contains my resume?"
        r1 = orchestrator.handle("which pdf contains my resume?")
        self.assertEqual(r1.plan.actions[0].tool, "search_file_contents")

        # Turn 2: User clarifies "its name is Akhil_Resume(Updated)"
        r2 = orchestrator.handle("its name is Akhil_Resume(Updated)")
        self.assertEqual(r2.plan.actions[0].tool, "find_files")
        self.assertEqual(r2.plan.actions[0].arguments.get("query"), "Akhil_Resume(Updated)")
        self.assertIn(".pdf", r2.plan.actions[0].arguments.get("extensions", []))

    # -------------------------------------------------------------
    # Milestone 4B.6: Safe File Opening & Contextual File Selection Tests
    # -------------------------------------------------------------

    def test_60_open_intent_detection(self):
        from ai.intent import extract_explicit_open_file_target
        res = extract_explicit_open_file_target("Open resume.pdf")
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "filename")
        self.assertEqual(res["filename"], "resume.pdf")

        res_it = extract_explicit_open_file_target("Open it")
        self.assertEqual(res_it, {"type": "pronoun", "target": "it"})

        res_first = extract_explicit_open_file_target("Open the first result")
        self.assertEqual(res_first, {"type": "index", "index": 1})

        res_num = extract_explicit_open_file_target("Open result 2")
        self.assertEqual(res_num, {"type": "index", "index": 2})

    def test_61_single_previous_result_open_it(self):
        resume = self.mock_downloads / "Akhil_Resume.pdf"
        resume.write_text("Resume Content")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # 1. Search produces 1 result
        r1 = orchestrator.handle("find Akhil_Resume.pdf in downloads")
        self.assertEqual(r1.plan.actions[0].tool, "find_files")

        # 2. "Open it" opens that exact single file
        with patch("os.startfile") as mock_startfile:
            r2 = orchestrator.handle("Open it")
            self.assertEqual(r2.plan.actions[0].tool, "open_file")
            self.assertEqual(r2.plan.actions[0].arguments["path"], str(resume.resolve()))
            mock_startfile.assert_called_once_with(str(resume.resolve()))

    def test_62_multiple_previous_results_open_it_clarification(self):
        (self.mock_downloads / "Resume.pdf").write_text("Resume 1")
        (self.mock_downloads / "Akhil_Resume.pdf").write_text("Resume 2")
        (self.mock_downloads / "Old_Resume.pdf").write_text("Resume 3")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r1 = orchestrator.handle("find pdf files in downloads")
        self.assertEqual(len(orchestrator.memory.get_last_file_results()), 3)

        # "Open it" must NOT guess; it must require clarification
        r2 = orchestrator.handle("Open it")
        self.assertTrue(r2.plan.needs_clarification)
        self.assertEqual(len(r2.plan.actions), 0)
        self.assertIn("multiple recent files", r2.plan.message)
        self.assertIn("Resume.pdf", r2.plan.message)

    def test_63_first_result_selection(self):
        (self.mock_downloads / "Resume.pdf").write_text("Resume 1")
        (self.mock_downloads / "Akhil_Resume.pdf").write_text("Resume 2")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.handle("find pdf files in downloads")

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("Open the first result")
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertIn("Resume.pdf", r.plan.actions[0].arguments["path"])
            mock_startfile.assert_called_once()

    def test_64_numbered_result_selection(self):
        f1 = self.mock_downloads / "doc1.txt"
        f2 = self.mock_downloads / "doc2.txt"
        f1.write_text("Doc 1")
        f2.write_text("Doc 2")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.handle("find txt files in downloads")

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("Open result 2")
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertIn("doc2.txt", r.plan.actions[0].arguments["path"])
            mock_startfile.assert_called_once()

    def test_65_invalid_index_selection(self):
        (self.mock_downloads / "doc1.txt").write_text("Doc 1")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.handle("find txt files in downloads")

        r = orchestrator.handle("Open result 10")
        self.assertTrue(r.plan.needs_clarification)
        self.assertEqual(len(r.plan.actions), 0)
        self.assertIn("Invalid selection", r.plan.message)

    def test_66_explicit_filename_exact_single_match(self):
        target = self.mock_documents / "my_single_doc.pdf"
        target.write_text("Single doc content")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("Open my_single_doc.pdf")
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertEqual(r.plan.actions[0].arguments["path"], str(target.resolve()))
            mock_startfile.assert_called_once_with(str(target.resolve()))

    def test_67_explicit_filename_multiple_matches(self):
        (self.mock_documents / "report.pdf").write_text("Report 1")
        (self.mock_downloads / "report.pdf").write_text("Report 2")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Open report.pdf")
        self.assertTrue(r.plan.needs_clarification)
        self.assertEqual(len(r.plan.actions), 0)
        self.assertIn("multiple files matching", r.plan.message)

    def test_68_explicit_filename_no_match(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Open non_existent_file_xyz.pdf")
        self.assertTrue(r.plan.needs_clarification)
        self.assertIn("Could not find any file named", r.plan.message)

    def test_69_unsafe_path_rejection(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle(r"Open C:\Windows\System32\something.exe")
        self.assertTrue(r.plan.needs_clarification)
        self.assertIn("outside the allowed locations", r.plan.message)
        self.assertEqual(len(r.plan.actions), 0)

    def test_70_path_traversal_rejection(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Open ../../Windows/file.exe")
        self.assertTrue(r.plan.needs_clarification)
        self.assertIn("outside the allowed locations", r.plan.message)
        self.assertEqual(len(r.plan.actions), 0)

    def test_71_browser_search_priority_preserved(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Search Google for Python tutorials")
        self.assertEqual(r.plan.actions[0].tool, "browser_search")

    def test_72_local_document_search_preserved(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Search my PDFs for botnet detection")
        self.assertEqual(r.plan.actions[0].tool, "search_file_contents")

    def test_73_dangerous_deletion_still_requires_approval(self):
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="dangerous_task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Delete all PDFs in Downloads")
        self.assertEqual(r.plan.risk_level, "high")
        self.assertTrue(r.plan.actions[0].requires_approval)

    # -------------------------------------------------------------
    # Milestone 4B.6.1 Specific Tests: Bug Fixes
    # -------------------------------------------------------------

    def test_74_open_application_separation_and_message_integrity(self):
        # Bug 1: Open Notepad routes to open_application and never mentions safe file locations
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(
            intent="open_application",
            message="Notepad could not be opened safely because it is outside the allowed locations: Desktop, Documents, Downloads.",
            actions=[ProposedAction(tool="open_application", arguments={"application": "Notepad"}, requires_approval=False, reason="launch")]
        )
        orchestrator = JarvisOrchestrator(llm=mock_llm)
        with patch("tools.application.launch_application") as mock_launch:
            mock_launch.return_value = MagicMock(success=True, message="Successfully launched Notepad.", error=None)
            r = orchestrator.handle("Open Notepad")
            self.assertEqual(r.plan.actions[0].tool, "open_application")
            self.assertEqual(r.plan.actions[0].arguments["application"], "Notepad")
            self.assertNotIn("outside the allowed locations", r.message)
            self.assertNotIn("Desktop, Documents, Downloads", r.message)
            self.assertEqual(r.message, "Opening Notepad.")

    def test_75_unsafe_file_path_remains_rejected(self):
        # Unsafe executable file paths remain rejected by file safety rules
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle(r"Open C:\Windows\System32\cmd.exe")
        self.assertTrue(r.plan.needs_clarification)
        self.assertIn("outside the allowed locations", r.plan.message)
        self.assertEqual(len(r.plan.actions), 0)

    def test_76_natural_filename_matching_multiple_matches_clarification(self):
        # Bug 2: "Open my resume" finds multiple resume files and does NOT guess
        (self.mock_downloads / "Akhil_Resume(Updated).pdf").write_text("Resume 1")
        (self.mock_downloads / "MyResume.pdf").write_text("Resume 2")
        (self.mock_downloads / "Resume_Akhil.pdf").write_text("Resume 3")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Open my resume")
        self.assertTrue(r.plan.needs_clarification)
        self.assertEqual(len(r.plan.actions), 0)
        self.assertIn("multiple files matching", r.plan.message.lower())
        self.assertIn("Akhil_Resume(Updated).pdf", r.plan.message)
        self.assertIn("MyResume.pdf", r.plan.message)

    def test_77_natural_filename_matching_single_match_opens_safely(self):
        # Bug 2: "Open my resume" with only one matching candidate opens it automatically
        target = self.mock_downloads / "suraj_resume.pdf"
        target.write_text("Suraj Resume Content")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("Open my resume")
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertEqual(r.plan.actions[0].arguments["path"], str(target.resolve()))
            mock_startfile.assert_called_once_with(str(target.resolve()))

    def test_78_exact_filename_priority_preserved(self):
        # Exact filename matching remains highest priority
        (self.mock_downloads / "Akhil_Resume(Updated).pdf").write_text("Updated Resume")
        (self.mock_downloads / "Resume.pdf").write_text("General Resume")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("Open Akhil_Resume(Updated).pdf")
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertIn("Akhil_Resume(Updated).pdf", r.plan.actions[0].arguments["path"])
            mock_startfile.assert_called_once()

    def test_79_unrelated_filename_returns_not_found(self):
        # Unrelated query still returns not found
        (self.mock_downloads / "budget.xlsx").write_text("Data")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Open my recipe")
        self.assertTrue(r.plan.needs_clarification)
        self.assertIn("Could not find any file named", r.plan.message)

    def test_80_large_ambiguity_bounded_display_and_total_count(self):
        # Bug 3: Large ambiguity list (e.g. 65 files) displays bounded preview and total count
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # Populate working memory with 65 results
        files_65 = [{"name": f"doc_{i}.pdf", "path": f"C:\\Users\\mock\\Downloads\\doc_{i}.pdf", "extension": ".pdf"} for i in range(1, 66)]
        orchestrator.memory.remember_file_results(files_65, total_count=65)

        r = orchestrator.handle("Open it")
        self.assertTrue(r.plan.needs_clarification)
        self.assertEqual(len(r.plan.actions), 0)
        self.assertIn("I found 65 matching files, so \"Open it\" is ambiguous.", r.plan.message)
        self.assertIn("Showing the first 10 of 65 results:", r.plan.message)
        self.assertIn("1. doc_1.pdf", r.plan.message)
        self.assertIn("10. doc_10.pdf", r.plan.message)
        self.assertNotIn("11. doc_11.pdf", r.plan.message)

    def test_81_index_selection_works_with_large_result_set(self):
        # "Open result 14" still works even when only first 10 were displayed
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        doc14 = self.mock_downloads / "doc_14.pdf"
        doc14.write_text("Doc 14 Content")

        files_65 = [
            {"name": f"doc_{i}.pdf", "path": str(doc14.resolve()) if i == 14 else f"C:\\Users\\mock\\Downloads\\doc_{i}.pdf", "extension": ".pdf"}
            for i in range(1, 66)
        ]
        orchestrator.memory.remember_file_results(files_65, total_count=65)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("Open result 14")
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertEqual(r.plan.actions[0].arguments["path"], str(doc14.resolve()))
            mock_startfile.assert_called_once_with(str(doc14.resolve()))

    def test_82_small_result_set_displays_normal_numbered_list(self):
        # Small result sets (e.g. 3 files) continue displaying normal numbered list
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        files_3 = [
            {"name": "file1.pdf", "path": "C:\\mock\\file1.pdf", "extension": ".pdf"},
            {"name": "file2.pdf", "path": "C:\\mock\\file2.pdf", "extension": ".pdf"},
            {"name": "file3.pdf", "path": "C:\\mock\\file3.pdf", "extension": ".pdf"},
        ]
        orchestrator.memory.remember_file_results(files_3, total_count=3)

        r = orchestrator.handle("Open it")
        self.assertTrue(r.plan.needs_clarification)
        self.assertIn("I found multiple recent files. Please specify which one you want to open:", r.plan.message)
        self.assertIn("1. file1.pdf", r.plan.message)
        self.assertIn("2. file2.pdf", r.plan.message)
        self.assertIn("3. file3.pdf", r.plan.message)
        self.assertNotIn("Showing the first 10", r.plan.message)

    # --- MILESTONE 4B.6.1 TESTS (Tests A through K, numbered 83 through 93) ---

    def test_83_test_a_open_bare_number_command(self):
        # Test A: Recent 5 results, input "open 5" -> open_file for item 5
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f5 = self.mock_downloads / "suraj resume.pdf"
        f5.write_text("Dummy content")

        recent_files = [
            {"name": "Akhil_Resume(Updated).pdf", "path": str(self.mock_downloads / "Akhil_Resume(Updated).pdf"), "extension": ".pdf"},
            {"name": "MyResume.pdf", "path": str(self.mock_downloads / "MyResume.pdf"), "extension": ".pdf"},
            {"name": "My_Resume.pdf", "path": str(self.mock_downloads / "My_Resume.pdf"), "extension": ".pdf"},
            {"name": "Resume_Akhil.pdf", "path": str(self.mock_downloads / "Resume_Akhil.pdf"), "extension": ".pdf"},
            {"name": "suraj resume.pdf", "path": str(f5.resolve()), "extension": ".pdf"},
        ]
        orchestrator.memory.remember_file_results(recent_files, total_count=5)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("open 5")
            self.assertEqual(len(r.plan.actions), 1)
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertEqual(r.plan.actions[0].arguments["path"], str(f5.resolve()))
            mock_startfile.assert_called_once_with(str(f5.resolve()))

    def test_84_test_b_bare_numeric_input_routes_to_file(self):
        # Test B: Input "5" -> open_file for item 5
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f5 = self.mock_downloads / "suraj resume.pdf"
        f5.write_text("Dummy content")

        recent_files = [
            {"name": f"file_{i}.pdf", "path": str(f5.resolve()) if i == 5 else f"C:\\mock\\file_{i}.pdf", "extension": ".pdf"}
            for i in range(1, 6)
        ]
        orchestrator.memory.remember_file_results(recent_files, total_count=5)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("5")
            self.assertEqual(len(r.plan.actions), 1)
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertEqual(r.plan.actions[0].arguments["path"], str(f5.resolve()))
            mock_startfile.assert_called_once_with(str(f5.resolve()))

    def test_85_test_c_open_hash_number_command(self):
        # Test C: Input "open #5" -> open_file for item 5
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f5 = self.mock_downloads / "suraj resume.pdf"
        f5.write_text("Dummy content")

        recent_files = [
            {"name": f"file_{i}.pdf", "path": str(f5.resolve()) if i == 5 else f"C:\\mock\\file_{i}.pdf", "extension": ".pdf"}
            for i in range(1, 6)
        ]
        orchestrator.memory.remember_file_results(recent_files, total_count=5)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("open #5")
            self.assertEqual(len(r.plan.actions), 1)
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertEqual(r.plan.actions[0].arguments["path"], str(f5.resolve()))
            mock_startfile.assert_called_once_with(str(f5.resolve()))

    def test_86_test_d_numeric_selection_out_of_bounds(self):
        # Test D: Input "open 6" when 5 exist -> bounds clarification message, no action
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        recent_files = [
            {"name": f"file_{i}.pdf", "path": f"C:\\mock\\file_{i}.pdf", "extension": ".pdf"}
            for i in range(1, 6)
        ]
        orchestrator.memory.remember_file_results(recent_files, total_count=5)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("open 6")
            self.assertEqual(len(r.plan.actions), 0)
            self.assertTrue(r.plan.needs_clarification)
            self.assertIn("Invalid selection", r.plan.message)
            self.assertIn("5 recent file results", r.plan.message)
            mock_startfile.assert_not_called()

    def test_87_test_e_large_result_set_numeric_selection(self):
        # Test E: Large result set (65 results), input "open 55" -> open_file for item 55
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f55 = self.mock_downloads / "doc_55.pdf"
        f55.write_text("Content 55")

        recent_files = [
            {"name": f"doc_{i}.pdf", "path": str(f55.resolve()) if i == 55 else f"C:\\mock\\doc_{i}.pdf", "extension": ".pdf"}
            for i in range(1, 66)
        ]
        orchestrator.memory.remember_file_results(recent_files, total_count=65)

        with patch("os.startfile") as mock_startfile:
            r = orchestrator.handle("open 55")
            self.assertEqual(len(r.plan.actions), 1)
            self.assertEqual(r.plan.actions[0].tool, "open_file")
            self.assertEqual(r.plan.actions[0].arguments["path"], str(f55.resolve()))
            mock_startfile.assert_called_once_with(str(f55.resolve()))

    def test_88_test_f_open_notepad_application(self):
        # Test F: Input "Open Notepad" -> action open_application with application="Notepad", no safe-file warning
        from core.executor import PlanExecutionResult, ActionExecutionResult
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        with patch.object(orchestrator.executor, "execute_plan") as mock_exec:
            mock_exec.return_value = PlanExecutionResult(results=[
                ActionExecutionResult(action_name="open_application", arguments={"application": "Notepad"}, executed=True, success=True, message="Opened Notepad")
            ])
            r = orchestrator.handle("Open Notepad")
            self.assertEqual(len(r.plan.actions), 1)
            self.assertEqual(r.plan.actions[0].tool, "open_application")
            self.assertEqual(r.plan.actions[0].arguments.get("application"), "Notepad")
            self.assertNotIn("safe-search locations", r.plan.message)

    def test_89_test_g_open_calculator_application_execution(self):
        # Test G: Input "Open Calculator" -> action open_application with application="Calculator", executor succeeds
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        with patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(poll=lambda: None)
            r = orchestrator.handle("Open Calculator")
            self.assertEqual(len(r.plan.actions), 1)
            self.assertEqual(r.plan.actions[0].tool, "open_application")
            self.assertEqual(r.plan.actions[0].arguments.get("application"), "Calculator")
            self.assertIsNotNone(r.execution_result)
            self.assertEqual(len(r.execution_result.results), 1)
            self.assertTrue(r.execution_result.results[0].success)

    def test_90_test_h_malformed_open_application_rejected_by_planner(self):
        # Test H: Malformed ProposedAction(tool="open_application", arguments={}) is rejected during validate_plan
        from core.planner import validate_plan
        from ai.schemas import TaskPlan, ProposedAction

        malformed_plan = TaskPlan(
            intent="task",
            message="Launching app",
            actions=[ProposedAction(tool="open_application", arguments={}, requires_approval=False, reason="Launch app")]
        )
        validated = validate_plan(malformed_plan)
        self.assertEqual(len(validated.actions), 0)

    def test_91_test_i_find_my_resume_in_downloads(self):
        # Test I: Available Akhil_Resume(Updated).pdf, input "Find my resume in Downloads" -> finds file
        resume_file = self.mock_downloads / "Akhil_Resume(Updated).pdf"
        resume_file.write_text("Sample resume content")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Find my resume in Downloads")
        self.assertEqual(len(r.plan.actions), 1)
        self.assertEqual(r.plan.actions[0].tool, "find_files")
        self.assertIsNotNone(r.execution_result)
        self.assertGreater(len(r.execution_result.results), 0)
        found_message = r.execution_result.results[0].message
        self.assertIn("Akhil_Resume(Updated).pdf", found_message)

    def test_92_test_j_find_the_resume_normalized_match(self):
        # Test J: Input "Find the resume" -> matches resume file using normalized query behavior
        resume_file = self.mock_downloads / "Akhil_Resume(Updated).pdf"
        resume_file.write_text("Sample resume content")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Find the resume")
        self.assertEqual(len(r.plan.actions), 1)
        self.assertEqual(r.plan.actions[0].tool, "find_files")
        self.assertIsNotNone(r.execution_result)
        self.assertGreater(len(r.execution_result.results), 0)
        found_message = r.execution_result.results[0].message
        self.assertIn("Akhil_Resume(Updated).pdf", found_message)

    def test_93_test_k_find_unrelated_unicorn_document_no_match(self):
        # Test K: Input "Find completely unrelated unicorn document" -> no matches
        resume_file = self.mock_downloads / "Akhil_Resume(Updated).pdf"
        resume_file.write_text("Sample resume content")

        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r = orchestrator.handle("Find completely unrelated unicorn document")
        self.assertEqual(len(r.plan.actions), 1)
        self.assertEqual(r.plan.actions[0].tool, "find_files")
        self.assertIsNotNone(r.execution_result)
        self.assertGreater(len(r.execution_result.results), 0)
        found_message = r.execution_result.results[0].message
        self.assertIn("No matching files found", found_message)

    def test_94_empty_application_recovery_in_validate_plan(self):
        # Empty application argument recovered deterministically from current_request
        from core.planner import validate_plan
        from ai.schemas import ProposedAction, TaskPlan
        plan = TaskPlan(
            intent="open_application",
            message="",
            actions=[ProposedAction(tool="open_application", arguments={}, requires_approval=False, reason="test")]
        )
        validated = validate_plan(plan, current_request="Open Calculator")
        self.assertEqual(len(validated.actions), 1)
        self.assertEqual(validated.actions[0].arguments["application"], "Calculator")

    def test_95_numeric_open_file_targets_extended(self):
        # Extended numeric and ordinal extraction patterns
        from ai.intent import extract_explicit_open_file_target
        self.assertEqual(extract_explicit_open_file_target("open number 5"), {"type": "index", "index": 5})
        self.assertEqual(extract_explicit_open_file_target("open result number 5"), {"type": "index", "index": 5})
        self.assertEqual(extract_explicit_open_file_target("open the 5th one"), {"type": "index", "index": 5})
        self.assertEqual(extract_explicit_open_file_target("open fifth one"), {"type": "index", "index": 5})
        self.assertEqual(extract_explicit_open_file_target("open the fifth result"), {"type": "index", "index": 5})
        self.assertEqual(extract_explicit_open_file_target("open 13"), {"type": "index", "index": 13})
        self.assertEqual(extract_explicit_open_file_target("open #14"), {"type": "index", "index": 14})

    def test_96_numeric_selection_without_file_results_clarification(self):
        # When user requests "open 5" without prior file results, return specific clarification
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)
        r = orchestrator.handle("open 5")
        self.assertTrue(r.plan.needs_clarification)
        self.assertIn("I don't have a recent file list to select result 5 from. Please search for files first or specify the file name.", r.message)

    def test_97_numeric_not_treated_as_application(self):
        from ai.intent import extract_explicit_open_app_target
        from tools.application import resolve_application
        self.assertIsNone(extract_explicit_open_app_target("open 5"))
        self.assertIsNone(extract_explicit_open_app_target("open 13"))
        self.assertIsNone(extract_explicit_open_app_target("open number 5"))
        self.assertIsNone(extract_explicit_open_app_target("open the 5th one"))
        self.assertIsNone(resolve_application("5"))
        self.assertIsNone(resolve_application("13"))
        self.assertIsNone(resolve_application("#14"))

    def test_98_test_a_new_clarification_replaces_old_result_context(self):
        # Test A: Store a large result set where item 1 is old_file.pdf.
        # Trigger a new natural-language clarification list where item 1 is new_resume.pdf.
        # User says "open 1" -> new_resume.pdf is opened, NEVER old_file.pdf.
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # 1. Store initial results
        old_file = self.mock_downloads / "old_file.pdf"
        old_file.write_text("old")
        orchestrator.memory.remember_file_results([
            {"name": "old_file.pdf", "path": str(old_file), "extension": ".pdf"},
            {"name": "other_file.pdf", "path": str(self.mock_downloads / "other_file.pdf"), "extension": ".pdf"},
        ])

        # Create new candidates for a resume search
        new_resume = self.mock_documents / "new_resume.pdf"
        new_resume.write_text("resume")
        second_resume = self.mock_documents / "second_resume.pdf"
        second_resume.write_text("resume2")

        # 2. Trigger clarification list
        r_clarif = orchestrator.handle("Open resume")
        self.assertTrue(r_clarif.plan.needs_clarification)
        self.assertIn("new_resume.pdf", r_clarif.message)
        self.assertIn("second_resume.pdf", r_clarif.message)

        # 3. User says "open 1"
        r_open = orchestrator.handle("open 1")
        self.assertFalse(r_open.plan.needs_clarification)
        self.assertEqual(len(r_open.plan.actions), 1)
        self.assertEqual(r_open.plan.actions[0].tool, "open_file")
        self.assertEqual(r_open.plan.actions[0].arguments["path"], str(new_resume))

    def test_99_test_b_resume_reproduction(self):
        # Test B: Resume reproduction
        # Simulate:
        # Find PDF files -> 65 results
        # Open my resume -> 5 resume candidates
        # open 1 -> Akhil_Resume(Updated).pdf
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # Populate 65 dummy files in memory
        dummy_pdfs = [
            {"name": f"10th MEMO (resized) (pdfresizer.com)_{i}.pdf", "path": str(self.mock_downloads / f"doc_{i}.pdf"), "extension": ".pdf"}
            for i in range(1, 66)
        ]
        orchestrator.memory.remember_file_results(dummy_pdfs, total_count=65)

        # Verify item 13 and item 14 open correctly from initial 65 results
        r13 = orchestrator.handle("open 13")
        self.assertEqual(r13.plan.actions[0].arguments["path"], str(self.mock_downloads / "doc_13.pdf"))
        r14 = orchestrator.handle("open 14")
        self.assertEqual(r14.plan.actions[0].arguments["path"], str(self.mock_downloads / "doc_14.pdf"))

        # Create 5 resume files in downloads
        resume_names = [
            "Akhil_Resume(Updated).pdf",
            "MyResume.pdf",
            "My_Resume.pdf",
            "Resume_Akhil.pdf",
            "suraj resume.pdf",
        ]
        created_paths = []
        for rname in resume_names:
            p = self.mock_downloads / rname
            p.write_text("dummy resume")
            created_paths.append(str(p))

        # User: "Open my resume"
        r_res = orchestrator.handle("Open my resume")
        self.assertTrue(r_res.plan.needs_clarification)
        self.assertIn("Akhil_Resume(Updated).pdf", r_res.message)

        # User: "open 1"
        r_open1 = orchestrator.handle("open 1")
        self.assertEqual(len(r_open1.plan.actions), 1)
        self.assertEqual(r_open1.plan.actions[0].tool, "open_file")
        self.assertEqual(r_open1.plan.actions[0].arguments["path"], created_paths[0])
        self.assertIn("Akhil_Resume(Updated).pdf", r_open1.plan.actions[0].arguments["path"])

    def test_100_test_c_all_selection_forms_use_current_clarification_context(self):
        # Test C: All selection forms (open 1, open result 1, open first, open first one)
        # after clarification resolve to file_a.pdf
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])

        file_a = self.mock_downloads / "candidate_report_a.pdf"
        file_a.write_text("A")
        file_b = self.mock_downloads / "candidate_report_b.pdf"
        file_b.write_text("B")
        file_c = self.mock_downloads / "candidate_report_c.pdf"
        file_c.write_text("C")

        selection_commands = ["open 1", "open result 1", "open first", "open first one"]
        for cmd in selection_commands:
            orchestrator = JarvisOrchestrator(llm=mock_llm)
            # Trigger clarification
            r_clarif = orchestrator.handle("Open candidate report")
            self.assertTrue(r_clarif.plan.needs_clarification)

            # Issue selection command
            r_sel = orchestrator.handle(cmd)
            self.assertFalse(r_sel.plan.needs_clarification, f"Failed on command: {cmd}")
            self.assertEqual(len(r_sel.plan.actions), 1, f"Failed on command: {cmd}")
            self.assertEqual(r_sel.plan.actions[0].arguments["path"], str(file_a), f"Failed on command: {cmd}")

    def test_101_test_d_new_context_replaces_old_context(self):
        # Test D: Clarification list A -> Clarification list B -> open 1 must open new_a.pdf
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        old_a = self.mock_downloads / "alpha_doc_one.doc"
        old_a.write_text("old a")
        old_b = self.mock_downloads / "alpha_doc_two.doc"
        old_b.write_text("old b")

        new_a = self.mock_documents / "beta_notes_one.pdf"
        new_a.write_text("new a")
        new_b = self.mock_documents / "beta_notes_two.pdf"
        new_b.write_text("new b")

        # Show list A
        r_a = orchestrator.handle("Open alpha doc")
        self.assertTrue(r_a.plan.needs_clarification)
        self.assertIn("alpha_doc_one.doc", r_a.message)

        # Show list B
        r_b = orchestrator.handle("Open beta notes")
        self.assertTrue(r_b.plan.needs_clarification)
        self.assertIn("beta_notes_one.pdf", r_b.message)

        # Select 1 -> must open new_a.pdf
        r_sel = orchestrator.handle("open 1")
        self.assertFalse(r_sel.plan.needs_clarification)
        self.assertEqual(r_sel.plan.actions[0].arguments["path"], str(new_a))

    def test_102_large_result_index_selectability_preserved(self):
        # Test large result preview (> 10 items) still allows selecting item 14
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # Simulate 65 files
        files_65 = [
            {"name": f"download_file_{i}.pdf", "path": str(self.mock_downloads / f"download_file_{i}.pdf"), "extension": ".pdf"}
            for i in range(1, 66)
        ]
        orchestrator.memory.remember_file_results(files_65, total_count=65)

        # User: "Open it" -> triggers ambiguous pronoun preview of first 10
        r_it = orchestrator.handle("Open it")
        self.assertTrue(r_it.plan.needs_clarification)
        self.assertIn("Showing the first 10 of 65 results:", r_it.message)

        # User: "open 14" -> opens item 14
        r_14 = orchestrator.handle("open 14")
        self.assertFalse(r_14.plan.needs_clarification)
        self.assertEqual(r_14.plan.actions[0].arguments["path"], str(self.mock_downloads / "download_file_14.pdf"))

    def test_103_ordinal_only_commands_with_active_context(self):
        # Ordinal-only commands: open first, open second, open third, open fifth
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        cands = [
            {"name": f"report_{i}.pdf", "path": str(self.mock_downloads / f"report_{i}.pdf"), "extension": ".pdf"}
            for i in range(1, 6)
        ]
        orchestrator.memory.remember_file_results(cands)

        # open first -> resolves index 1
        r1 = orchestrator.handle("open first")
        self.assertFalse(r1.plan.needs_clarification)
        self.assertEqual(r1.plan.actions[0].arguments["path"], str(self.mock_downloads / "report_1.pdf"))

        # open second -> resolves index 2
        r2 = orchestrator.handle("open second")
        self.assertFalse(r2.plan.needs_clarification)
        self.assertEqual(r2.plan.actions[0].arguments["path"], str(self.mock_downloads / "report_2.pdf"))

        # open third -> resolves index 3
        r3 = orchestrator.handle("open third")
        self.assertFalse(r3.plan.needs_clarification)
        self.assertEqual(r3.plan.actions[0].arguments["path"], str(self.mock_downloads / "report_3.pdf"))

        # open fifth -> resolves index 5
        r5 = orchestrator.handle("open fifth")
        self.assertFalse(r5.plan.needs_clarification)
        self.assertEqual(r5.plan.actions[0].arguments["path"], str(self.mock_downloads / "report_5.pdf"))

    def test_104_equivalent_selection_forms(self):
        # Verify equivalent selection forms resolve to the same candidate:
        # open 1, open result 1, open first, open first one
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        cands = [
            {"name": f"resume_{i}.pdf", "path": str(self.mock_downloads / f"resume_{i}.pdf"), "extension": ".pdf"}
            for i in range(1, 6)
        ]
        orchestrator.memory.remember_file_results(cands)

        target_path = str(self.mock_downloads / "resume_1.pdf")
        for cmd in ["open 1", "open result 1", "open first", "open first one"]:
            res = orchestrator.handle(cmd)
            self.assertFalse(res.plan.needs_clarification, f"Failed on command: {cmd}")
            self.assertEqual(len(res.plan.actions), 1, f"Failed on command: {cmd}")
            self.assertEqual(res.plan.actions[0].arguments["path"], target_path, f"Failed on command: {cmd}")

    def test_105_no_context_safety_for_ordinals(self):
        # Without an active file-selection context:
        # open first -> 0 actions, asks for clarification
        # open fifth -> 0 actions, asks for clarification
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="invented action", actions=[
            ProposedAction(tool="open_application", arguments={"application": "notepad"}, requires_approval=False, reason="hallucinated")
        ])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        r_first = orchestrator.handle("open first")
        self.assertTrue(r_first.plan.needs_clarification)
        self.assertEqual(len(r_first.plan.actions), 0)
        self.assertIn("I don't have a recent file list to select result 1 from", r_first.message)
        mock_llm.create_plan.assert_not_called()

        r_fifth = orchestrator.handle("open fifth")
        self.assertTrue(r_fifth.plan.needs_clarification)
        self.assertEqual(len(r_fifth.plan.actions), 0)
        self.assertIn("I don't have a recent file list to select result 5 from", r_fifth.message)
        mock_llm.create_plan.assert_not_called()

    def test_106_llm_bypass_guard(self):
        # With active selection context, LLM is never called and cannot inject hallucinated open_application actions
        mock_llm = MagicMock()
        # Even if mock_llm were called, it would try to return multiple hallucinated open_application actions
        mock_llm.create_plan.return_value = TaskPlan(
            intent="task",
            message="Hallucinated plan",
            actions=[
                ProposedAction(tool="open_application", arguments={"application": "calc"}, requires_approval=False, reason="fake 1"),
                ProposedAction(tool="open_application", arguments={"application": "notepad"}, requires_approval=False, reason="fake 2"),
            ]
        )
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        cands = [
            {"name": "Akhil_Resume(Updated).pdf", "path": str(self.mock_downloads / "Akhil_Resume(Updated).pdf"), "extension": ".pdf"},
            {"name": "MyResume.pdf", "path": str(self.mock_downloads / "MyResume.pdf"), "extension": ".pdf"},
        ]
        orchestrator.memory.remember_file_results(cands)

        # open first
        res = orchestrator.handle("open first")
        mock_llm.create_plan.assert_not_called()
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "open_file")
        self.assertEqual(res.plan.actions[0].arguments["path"], str(self.mock_downloads / "Akhil_Resume(Updated).pdf"))

    # =========================================================================
    # Milestone 4C.1: Conversational File Reference Resolution Tests
    # =========================================================================

    def test_107_scenario_a_single_result_pronoun_resolution(self):
        # Scenario A: Exactly one file in context -> "Open it", "Open that file", "Open that one"
        # selects the file without calling LLM.
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        botnet_pdf = self.mock_documents / "Botnet_Detection.pdf"
        botnet_pdf.write_text("botnet analysis")
        orchestrator.memory.remember_file_results([
            {"name": "Botnet_Detection.pdf", "path": str(botnet_pdf), "extension": ".pdf"}
        ])

        for phrase in ["Open it", "Open that file", "Open that one"]:
            res = orchestrator.handle(phrase)
            mock_llm.create_plan.assert_not_called()
            self.assertFalse(res.plan.needs_clarification, f"Failed on phrase: {phrase}")
            self.assertEqual(len(res.plan.actions), 1, f"Failed on phrase: {phrase}")
            self.assertEqual(res.plan.actions[0].tool, "open_file")
            self.assertEqual(res.plan.actions[0].arguments["path"], str(botnet_pdf))

    def test_108_scenario_b_multiple_result_pronoun_ambiguity(self):
        # Scenario B: Multiple candidates in context -> "Open it" requests clarification, opens nothing, no LLM actions.
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.remember_file_results([
            {"name": "Resume_1.pdf", "path": str(self.mock_downloads / "Resume_1.pdf"), "extension": ".pdf"},
            {"name": "Resume_2.pdf", "path": str(self.mock_downloads / "Resume_2.pdf"), "extension": ".pdf"},
        ])

        for phrase in ["Open it", "Open that file", "Open that one"]:
            res = orchestrator.handle(phrase)
            mock_llm.create_plan.assert_not_called()
            self.assertTrue(res.plan.needs_clarification, f"Failed on phrase: {phrase}")
            self.assertEqual(len(res.plan.actions), 0, f"Failed on phrase: {phrase}")
            self.assertIn("I found multiple recent files", res.message)

    def test_109_scenario_f_named_active_context_match(self):
        # Scenario F: Match named file directly in active context (case-insensitive)
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        resume_path = str(self.mock_downloads / "Akhil_Resume(Updated).pdf")
        orchestrator.memory.remember_file_results([
            {"name": "Akhil_Resume(Updated).pdf", "path": resume_path, "extension": ".pdf"},
            {"name": "MyResume.pdf", "path": str(self.mock_downloads / "MyResume.pdf"), "extension": ".pdf"},
        ])

        res = orchestrator.handle("Open Akhil_Resume(Updated).pdf")
        mock_llm.create_plan.assert_not_called()
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].arguments["path"], resume_path)

        # Case-insensitive / formatted name
        res2 = orchestrator.handle("Open akhil_resume(updated).pdf")
        self.assertFalse(res2.plan.needs_clarification)
        self.assertEqual(res2.plan.actions[0].arguments["path"], resume_path)

    def test_110_scenario_g_unique_descriptive_reference(self):
        # Scenario G: "Open the updated one" when exactly one candidate has "updated"
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        updated_path = str(self.mock_downloads / "Akhil_Resume(Updated).pdf")
        orchestrator.memory.remember_file_results([
            {"name": "Akhil_Resume(Updated).pdf", "path": updated_path, "extension": ".pdf"},
            {"name": "MyResume.pdf", "path": str(self.mock_downloads / "MyResume.pdf"), "extension": ".pdf"},
            {"name": "My_Resume.pdf", "path": str(self.mock_downloads / "My_Resume.pdf"), "extension": ".pdf"},
        ])

        res = orchestrator.handle("Open the updated one")
        mock_llm.create_plan.assert_not_called()
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].arguments["path"], updated_path)

    def test_111_scenario_h_ambiguous_descriptive_reference(self):
        # Scenario H: "Open the updated one" when multiple candidates have "updated" -> clarification
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.remember_file_results([
            {"name": "Resume_Updated.pdf", "path": str(self.mock_downloads / "Resume_Updated.pdf"), "extension": ".pdf"},
            {"name": "Updated_Resume_Final.pdf", "path": str(self.mock_downloads / "Updated_Resume_Final.pdf"), "extension": ".pdf"},
        ])

        res = orchestrator.handle("Open the updated one")
        mock_llm.create_plan.assert_not_called()
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("multiple files matching 'updated'", res.message)

    def test_112_scenario_i_no_context_pronoun_reference(self):
        # Scenario I: "Open it" with no active file context -> safe clarification, 0 actions, no LLM hallucination
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open it")
        mock_llm.create_plan.assert_not_called()
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("I don't have a recent file result to resolve \"it\" to", res.message)

    def test_113_recently_opened_file_open_it_again(self):
        # "Open it again" re-opens the most recently opened file
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        doc = self.mock_documents / "project_specs.pdf"
        doc.write_text("specs")
        orchestrator.memory.remember_file_results([
            {"name": "project_specs.pdf", "path": str(doc), "extension": ".pdf"}
        ])

        # 1. Open it
        res1 = orchestrator.handle("Open it")
        self.assertFalse(res1.plan.needs_clarification)
        self.assertEqual(res1.plan.actions[0].arguments["path"], str(doc))

        # 2. Open it again
        res2 = orchestrator.handle("Open it again")
        self.assertFalse(res2.plan.needs_clarification)
        self.assertEqual(len(res2.plan.actions), 1)
        self.assertEqual(res2.plan.actions[0].arguments["path"], str(doc))

    def test_114_descriptive_called_named_reference(self):
        # "Open the resume called Akhil" matches Akhil_Resume(Updated).pdf in active context
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        akhil_doc = str(self.mock_downloads / "Akhil_Resume(Updated).pdf")
        orchestrator.memory.remember_file_results([
            {"name": "Akhil_Resume(Updated).pdf", "path": akhil_doc, "extension": ".pdf"},
            {"name": "MyResume.pdf", "path": str(self.mock_downloads / "MyResume.pdf"), "extension": ".pdf"},
        ])

        res = orchestrator.handle("Open the resume called Akhil")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].arguments["path"], akhil_doc)

    # ---------------------------------------------------------
    # Milestone 4C.2: Conversational File Navigation & Contextual Selection
    # ---------------------------------------------------------

    def test_115_test_a_next_file_navigation(self):
        # Test A: Context [A.pdf, B.pdf, C.pdf], Current=2, Command: "Open the next one" -> opens C.pdf
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        a_pdf = str(self.mock_documents / "A.pdf")
        b_pdf = str(self.mock_documents / "B.pdf")
        c_pdf = str(self.mock_documents / "C.pdf")
        Path(a_pdf).write_text("a")
        Path(b_pdf).write_text("b")
        Path(c_pdf).write_text("c")

        orchestrator.memory.remember_file_results([
            {"name": "A.pdf", "path": a_pdf, "extension": ".pdf"},
            {"name": "B.pdf", "path": b_pdf, "extension": ".pdf"},
            {"name": "C.pdf", "path": c_pdf, "extension": ".pdf"},
        ])
        orchestrator.memory.set_current_file_selection_index(2)

        res = orchestrator.handle("Open the next one")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].arguments["path"], c_pdf)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 3)
        self.assertFalse(mock_llm.create_plan.called)

    def test_116_test_b_previous_file_navigation(self):
        # Test B: Context [A.pdf, B.pdf, C.pdf], Current=2, Command: "Open the previous one" -> opens A.pdf
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        a_pdf = str(self.mock_documents / "A.pdf")
        b_pdf = str(self.mock_documents / "B.pdf")
        c_pdf = str(self.mock_documents / "C.pdf")
        Path(a_pdf).write_text("a")
        Path(b_pdf).write_text("b")
        Path(c_pdf).write_text("c")

        orchestrator.memory.remember_file_results([
            {"name": "A.pdf", "path": a_pdf, "extension": ".pdf"},
            {"name": "B.pdf", "path": b_pdf, "extension": ".pdf"},
            {"name": "C.pdf", "path": c_pdf, "extension": ".pdf"},
        ])
        orchestrator.memory.set_current_file_selection_index(2)

        res = orchestrator.handle("Open the previous one")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].arguments["path"], a_pdf)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 1)
        self.assertFalse(mock_llm.create_plan.called)

    def test_117_test_c_next_boundary_safety(self):
        # Test C: Current=3 of 3, Command: "Open next" -> 0 actions, safe boundary message, 0 LLM calls
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.remember_file_results([
            {"name": "A.pdf", "path": str(self.mock_documents / "A.pdf"), "extension": ".pdf"},
            {"name": "B.pdf", "path": str(self.mock_documents / "B.pdf"), "extension": ".pdf"},
            {"name": "C.pdf", "path": str(self.mock_documents / "C.pdf"), "extension": ".pdf"},
        ])
        orchestrator.memory.set_current_file_selection_index(3)

        res = orchestrator.handle("Open next")
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("already at the last file", res.plan.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_118_test_d_previous_boundary_safety(self):
        # Test D: Current=1 of 3, Command: "Open previous" -> 0 actions, safe boundary message, 0 LLM calls
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.remember_file_results([
            {"name": "A.pdf", "path": str(self.mock_documents / "A.pdf"), "extension": ".pdf"},
            {"name": "B.pdf", "path": str(self.mock_documents / "B.pdf"), "extension": ".pdf"},
            {"name": "C.pdf", "path": str(self.mock_documents / "C.pdf"), "extension": ".pdf"},
        ])
        orchestrator.memory.set_current_file_selection_index(1)

        res = orchestrator.handle("Open previous")
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("already at the first file", res.plan.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_119_test_e_no_context_safety(self):
        # Test E: No active selection context, Command: "Open the next one" -> clarification, 0 actions, 0 LLM calls
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.clear()
        res = orchestrator.handle("Open the next one")
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("I don't have a recent file list to determine what \"next\" refers to", res.plan.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_120_test_f_no_current_selection_safety(self):
        # Test F: Context exists but current_index is None, Command: "Open next" -> prompt to specify start file
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.remember_file_results([
            {"name": "A.pdf", "path": str(self.mock_documents / "A.pdf"), "extension": ".pdf"},
            {"name": "B.pdf", "path": str(self.mock_documents / "B.pdf"), "extension": ".pdf"},
        ])
        self.assertIsNone(orchestrator.memory.get_current_file_selection_index())

        res = orchestrator.handle("Open next")
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("Please specify which file you want to start with from the current list", res.plan.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_121_test_g_numeric_selection_then_next(self):
        # Test G: Command: "Open 2" -> opens B.pdf, current_index=2. Then "Open next" -> opens C.pdf, current_index=3
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        a_pdf = str(self.mock_documents / "A.pdf")
        b_pdf = str(self.mock_documents / "B.pdf")
        c_pdf = str(self.mock_documents / "C.pdf")
        Path(a_pdf).write_text("a")
        Path(b_pdf).write_text("b")
        Path(c_pdf).write_text("c")

        orchestrator.memory.remember_file_results([
            {"name": "A.pdf", "path": a_pdf, "extension": ".pdf"},
            {"name": "B.pdf", "path": b_pdf, "extension": ".pdf"},
            {"name": "C.pdf", "path": c_pdf, "extension": ".pdf"},
        ])

        # Step 1: Open 2
        res1 = orchestrator.handle("Open 2")
        self.assertFalse(res1.plan.needs_clarification)
        self.assertEqual(res1.plan.actions[0].arguments["path"], b_pdf)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 2)

        # Step 2: Open next
        res2 = orchestrator.handle("Open next")
        self.assertFalse(res2.plan.needs_clarification)
        self.assertEqual(res2.plan.actions[0].arguments["path"], c_pdf)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 3)
        self.assertFalse(mock_llm.create_plan.called)

    def test_122_test_h_descriptive_selection_then_navigation(self):
        # Test H: "Open the updated one" -> opens Resume_Updated.pdf (index 2). Then "Open next" -> opens Project.pdf (index 3)
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        p1 = str(self.mock_documents / "Resume.pdf")
        p2 = str(self.mock_documents / "Resume_Updated.pdf")
        p3 = str(self.mock_documents / "Project.pdf")
        Path(p1).write_text("1")
        Path(p2).write_text("2")
        Path(p3).write_text("3")

        orchestrator.memory.remember_file_results([
            {"name": "Resume.pdf", "path": p1, "extension": ".pdf"},
            {"name": "Resume_Updated.pdf", "path": p2, "extension": ".pdf"},
            {"name": "Project.pdf", "path": p3, "extension": ".pdf"},
        ])

        res1 = orchestrator.handle("Open the updated one")
        self.assertFalse(res1.plan.needs_clarification)
        self.assertEqual(res1.plan.actions[0].arguments["path"], p2)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 2)

        res2 = orchestrator.handle("Open next")
        self.assertFalse(res2.plan.needs_clarification)
        self.assertEqual(res2.plan.actions[0].arguments["path"], p3)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 3)
        self.assertFalse(mock_llm.create_plan.called)

    def test_123_test_i_context_replacement(self):
        # Test I: Context A (current_index=2), replaced by Context B -> current_index safely reset
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.remember_file_results([
            {"name": "A.pdf", "path": str(self.mock_documents / "A.pdf"), "extension": ".pdf"},
            {"name": "B.pdf", "path": str(self.mock_documents / "B.pdf"), "extension": ".pdf"},
            {"name": "C.pdf", "path": str(self.mock_documents / "C.pdf"), "extension": ".pdf"},
        ])
        orchestrator.memory.set_current_file_selection_index(2)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 2)

        # Replace with Context B
        orchestrator.memory.remember_file_results([
            {"name": "Resume1.pdf", "path": str(self.mock_documents / "Resume1.pdf"), "extension": ".pdf"},
            {"name": "Resume2.pdf", "path": str(self.mock_documents / "Resume2.pdf"), "extension": ".pdf"},
        ])
        # Invariant: current_index is safely reset upon active context replacement
        self.assertIsNone(orchestrator.memory.get_current_file_selection_index())

        res = orchestrator.handle("Open next")
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("Please specify which file you want to start with from the current list", res.plan.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_124_test_j_relative_variants_and_llm_bypass(self):
        # Test J: All navigation variants bypass LLM planning and resolve properly
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f1 = str(self.mock_documents / "Doc1.pdf")
        f2 = str(self.mock_documents / "Doc2.pdf")
        Path(f1).write_text("1")
        Path(f2).write_text("2")

        orchestrator.memory.remember_file_results([
            {"name": "Doc1.pdf", "path": f1, "extension": ".pdf"},
            {"name": "Doc2.pdf", "path": f2, "extension": ".pdf"},
        ])

        # "Open first" establishes index 1
        res = orchestrator.handle("Open first")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 1)

        # "Open the one after that" -> Doc2.pdf
        res2 = orchestrator.handle("Open the one after that")
        self.assertFalse(res2.plan.needs_clarification)
        self.assertEqual(res2.plan.actions[0].arguments["path"], f2)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 2)

        # "Open the one before that" -> Doc1.pdf
        res3 = orchestrator.handle("Open the one before that")
        self.assertFalse(res3.plan.needs_clarification)
        self.assertEqual(res3.plan.actions[0].arguments["path"], f1)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 1)

        # "Open the next PDF" -> Doc2.pdf
        res4 = orchestrator.handle("Open the next PDF")
        self.assertFalse(res4.plan.needs_clarification)
        self.assertEqual(res4.plan.actions[0].arguments["path"], f2)
        self.assertEqual(orchestrator.memory.get_current_file_selection_index(), 2)

        # Check boundary with file-type: "Open the next PDF"
        res5 = orchestrator.handle("Open the next PDF")
        self.assertTrue(res5.plan.needs_clarification)
        self.assertIn("already at the last pdf", res5.plan.message)

        self.assertFalse(mock_llm.create_plan.called)

    # ---------------------------------------------------------
    # Milestone 4C.3: Conversational File Actions Using Last-Opened File Context
    # ---------------------------------------------------------

    def test_125_test_a_file_name_conversational_action(self):
        # Test A: "What is its name?" returns the filename, 0 LLM calls, 0 computer actions
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f_path = str(self.mock_documents / "Akhil_Resume(Updated).pdf")
        orchestrator.memory.set_last_opened_file({
            "name": "Akhil_Resume(Updated).pdf",
            "path": f_path,
            "extension": ".pdf",
        })

        res = orchestrator.handle("What is its name?")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("Akhil_Resume(Updated).pdf", res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_126_test_b_file_type_conversational_action(self):
        # Test B: "What type of file is it?" returns PDF / .pdf response, 0 LLM calls, 0 computer actions
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f_path = str(self.mock_documents / "project_report.pdf")
        orchestrator.memory.set_last_opened_file({
            "name": "project_report.pdf",
            "path": f_path,
            "extension": ".pdf",
        })

        res = orchestrator.handle("What type of file is it?")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("PDF", res.message)
        self.assertIn(".pdf", res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_127_test_c_type_check_true(self):
        # Test C: "Is this a PDF?" on PDF -> Yes, 0 LLM calls, 0 actions
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f_path = str(self.mock_documents / "project_report.pdf")
        orchestrator.memory.set_last_opened_file({
            "name": "project_report.pdf",
            "path": f_path,
            "extension": ".pdf",
        })

        res = orchestrator.handle("Is this a PDF?")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertTrue(res.message.startswith("Yes"))
        self.assertIn("PDF", res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_128_test_d_type_check_false(self):
        # Test D: "Is this a PDF?" on DOCX -> No, 0 LLM calls, 0 actions
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f_path = str(self.mock_documents / "meeting_notes.docx")
        orchestrator.memory.set_last_opened_file({
            "name": "meeting_notes.docx",
            "path": f_path,
            "extension": ".docx",
        })

        res = orchestrator.handle("Is this a PDF?")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertTrue(res.message.startswith("No"))
        self.assertIn("DOCX", res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_129_test_e_location(self):
        # Test E: "Where is it?" returns containing directory, 0 LLM calls, 0 actions
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f_path = str(self.mock_documents / "spec.txt")
        orchestrator.memory.set_last_opened_file({
            "name": "spec.txt",
            "path": f_path,
            "extension": ".txt",
        })

        res = orchestrator.handle("Where is it?")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn(str(self.mock_documents), res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_130_test_f_reveal_location(self):
        # Test F: "Show me where it is" emits safe reveal_file action, 0 LLM calls
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f_file = self.mock_documents / "spec.txt"
        f_file.write_text("content")
        f_path = str(f_file)
        orchestrator.memory.set_last_opened_file({
            "name": "spec.txt",
            "path": f_path,
            "extension": ".txt",
        })

        with patch("subprocess.Popen") as mock_popen:
            res = orchestrator.handle("Show me where it is")
            self.assertFalse(res.plan.needs_clarification)
            self.assertEqual(len(res.plan.actions), 1)
            self.assertEqual(res.plan.actions[0].tool, "reveal_file")
            self.assertEqual(res.plan.actions[0].arguments["path"], f_path)
            self.assertFalse(mock_llm.create_plan.called)
            self.assertTrue(mock_popen.called)

    def test_131_test_g_no_context_name(self):
        # Test G: "What is its name?" without last opened file -> clarification, 0 actions, 0 LLM calls
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.clear()
        res = orchestrator.handle("What is its name?")
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("I don't have a recently opened file to determine what \"it\" refers to", res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_132_test_h_no_context_location(self):
        # Test H: "Where is it?" without last opened file -> clarification, 0 actions, 0 LLM calls
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        orchestrator.memory.clear()
        res = orchestrator.handle("Where is it?")
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("I don't have a recently opened file to determine what \"it\" refers to", res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_133_test_i_stale_file_reveal(self):
        # Test I: Stale file (deleted) -> safe stale-file message, 0 actions, 0 LLM calls
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        stale_path = str(self.mock_documents / "deleted_file.pdf")
        orchestrator.memory.set_last_opened_file({
            "name": "deleted_file.pdf",
            "path": stale_path,
            "extension": ".pdf",
        })

        res = orchestrator.handle("Show me where it is")
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("no longer exists", res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_134_test_j_regression_protection(self):
        # Test J: Verify existing commands (Open it again, Open next, Open previous, Open 1, Open the updated one) still work
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(intent="task", message="LLM hallucination", actions=[])
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        p1 = str(self.mock_documents / "Doc1.pdf")
        p2 = str(self.mock_documents / "Doc2_Updated.pdf")
        Path(p1).write_text("1")
        Path(p2).write_text("2")

        orchestrator.memory.remember_file_results([
            {"name": "Doc1.pdf", "path": p1, "extension": ".pdf"},
            {"name": "Doc2_Updated.pdf", "path": p2, "extension": ".pdf"},
        ])

        # 1. Open 1
        res1 = orchestrator.handle("Open 1")
        self.assertFalse(res1.plan.needs_clarification)
        self.assertEqual(res1.plan.actions[0].arguments["path"], p1)

        # 2. Open it again
        res2 = orchestrator.handle("Open it again")
        self.assertFalse(res2.plan.needs_clarification)
        self.assertEqual(res2.plan.actions[0].arguments["path"], p1)

        # 3. Open next
        res3 = orchestrator.handle("Open next")
        self.assertFalse(res3.plan.needs_clarification)
        self.assertEqual(res3.plan.actions[0].arguments["path"], p2)

        # 4. Open previous
        res4 = orchestrator.handle("Open previous")
        self.assertFalse(res4.plan.needs_clarification)
        self.assertEqual(res4.plan.actions[0].arguments["path"], p1)

        # 5. Open the updated one
        res5 = orchestrator.handle("Open the updated one")
        self.assertFalse(res5.plan.needs_clarification)
        self.assertEqual(res5.plan.actions[0].arguments["path"], p2)

        self.assertFalse(mock_llm.create_plan.called)

    # =========================================================================
    # Milestone 4D.1 - Natural Language Application & Folder Control Tests
    # =========================================================================

    def test_135_test_a_open_supported_application(self):
        # Test A: "Open Chrome" launches chrome deterministically without calling LLM
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open Chrome")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "open_application")
        self.assertEqual(res.plan.actions[0].arguments["application"], "Google Chrome")
        self.assertFalse(mock_llm.create_plan.called)

    def test_136_test_b_open_application_aliases(self):
        # Test B: Aliases like "Open VS Code", "Launch Notepad", "Open Calc"
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # 1. VS Code alias
        res1 = orchestrator.handle("Open VS Code")
        self.assertEqual(res1.plan.actions[0].tool, "open_application")
        self.assertEqual(res1.plan.actions[0].arguments["application"], "Visual Studio Code")

        # 2. Launch Notepad
        res2 = orchestrator.handle("Launch Notepad")
        self.assertEqual(res2.plan.actions[0].tool, "open_application")
        self.assertEqual(res2.plan.actions[0].arguments["application"], "Notepad")

        # 3. Open Calc alias
        res3 = orchestrator.handle("Open Calc")
        self.assertEqual(res3.plan.actions[0].tool, "open_application")
        self.assertEqual(res3.plan.actions[0].arguments["application"], "Calculator")

        # 4. Open File Explorer alias
        res4 = orchestrator.handle("Open File Explorer")
        self.assertEqual(res4.plan.actions[0].tool, "open_application")
        self.assertEqual(res4.plan.actions[0].arguments["application"], "File Explorer")

        self.assertFalse(mock_llm.create_plan.called)

    def test_137_test_c_unknown_application_rejection(self):
        # Test C: "Open SomeRandomUnknownApp" executes and fails safely through allowlist
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open SomeRandomUnknownApp")
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "open_application")
        self.assertEqual(res.plan.actions[0].arguments["application"], "SomeRandomUnknownApp")
        self.assertIsNotNone(res.execution_result)
        self.assertFalse(res.execution_result.results[0].success)
        self.assertEqual(res.execution_result.results[0].error, "UNKNOWN_APPLICATION")
        self.assertIn("not in the allowed application list", res.execution_result.results[0].message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_138_test_d_browser_search_preservation(self):
        # Test D: "Search for Chrome extensions" and "Search for VS Code tutorials" must remain web searches
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        # Query 1
        res1 = orchestrator.handle("Search for Chrome extensions")
        self.assertEqual(len(res1.plan.actions), 1)
        self.assertEqual(res1.plan.actions[0].tool, "browser_search")
        self.assertEqual(res1.plan.actions[0].arguments["query"], "Chrome extensions")

        # Query 2
        res2 = orchestrator.handle("Search for VS Code tutorials")
        self.assertEqual(len(res2.plan.actions), 1)
        self.assertEqual(res2.plan.actions[0].tool, "browser_search")
        self.assertEqual(res2.plan.actions[0].arguments["query"], "VS Code tutorials")

        self.assertFalse(mock_llm.create_plan.called)

    def test_139_test_e_open_downloads_folder(self):
        # Test E: "Open Downloads" and "Open my Downloads folder" safely opens downloads folder
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open Downloads")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "open_folder")
        self.assertEqual(res.plan.actions[0].arguments["folder"], "downloads")

        res_long = orchestrator.handle("Open my Downloads folder")
        self.assertEqual(res_long.plan.actions[0].tool, "open_folder")
        self.assertEqual(res_long.plan.actions[0].arguments["folder"], "downloads")

        self.assertFalse(mock_llm.create_plan.called)

    def test_140_test_f_open_documents_folder(self):
        # Test F: "Open Documents" and "Open my Documents"
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open my Documents")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "open_folder")
        self.assertEqual(res.plan.actions[0].arguments["folder"], "documents")
        self.assertFalse(mock_llm.create_plan.called)

    def test_141_test_g_open_desktop_folder(self):
        # Test G: "Open Desktop" and "Show my Desktop folder"
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Show my Desktop folder")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "open_folder")
        self.assertEqual(res.plan.actions[0].arguments["folder"], "desktop")
        self.assertFalse(mock_llm.create_plan.called)

    def test_142_test_h_arbitrary_folder_safety(self):
        # Test H: Arbitrary system paths like "C:\Windows\System32" are strictly rejected as UNSAFE_LOCATION
        from tools.files import open_local_folder
        res = open_local_folder(r"C:\Windows\System32")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "UNSAFE_LOCATION")
        self.assertIn("outside the allowed locations", res.message)

    def test_143_test_i_contextual_file_folder_disambiguation(self):
        # Test I: "Open its folder" refers to last opened file, NOT a generic folder
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        test_pdf = self.mock_downloads / "Report.pdf"
        test_pdf.write_text("dummy")

        orchestrator.memory.set_last_opened_file({
            "name": "Report.pdf",
            "path": str(test_pdf),
            "extension": ".pdf",
        })

        res = orchestrator.handle("Open its folder")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 1)
        self.assertEqual(res.plan.actions[0].tool, "reveal_file")
        self.assertEqual(res.plan.actions[0].arguments["path"], str(test_pdf))
        self.assertFalse(mock_llm.create_plan.called)

    def test_144_test_j_existing_milestone_regressions(self):
        # Test J: Preserve file opening, ordinal resolution, and navigation
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f1 = self.mock_documents / "Akhil_Resume(Updated).pdf"
        f2 = self.mock_documents / "MyResume.pdf"
        f1.write_text("resume1")
        f2.write_text("resume2")

        # Clarification triggers
        r_clarif = orchestrator.handle("Open my resume")
        self.assertTrue(r_clarif.plan.needs_clarification)

        # Numeric selection
        r_sel = orchestrator.handle("open 1")
        self.assertEqual(r_sel.plan.actions[0].tool, "open_file")
        self.assertEqual(r_sel.plan.actions[0].arguments["path"], str(f1))

        # Relative navigation
        r_next = orchestrator.handle("open next")
        self.assertEqual(r_next.plan.actions[0].tool, "open_file")
        self.assertEqual(r_next.plan.actions[0].arguments["path"], str(f2))

        # Reopen
        r_again = orchestrator.handle("open it again")
        self.assertEqual(r_again.plan.actions[0].tool, "open_file")
        self.assertEqual(r_again.plan.actions[0].arguments["path"], str(f2))

        # Last opened file question
        r_what = orchestrator.handle("What is its name?")
        self.assertIn("MyResume.pdf", r_what.message)

    # =========================================================================
    # Milestone 4D.2 - Multi-Step Task Understanding & Ordered Execution Tests
    # =========================================================================

    def test_145_test_a_app_plus_search(self):
        # Test A: "Open Chrome and search for Python tutorials"
        # 2 actions, correct order, no LLM required, both execute safely
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open Chrome and search for Python tutorials")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 2)
        # Action 1: open_application("Google Chrome")
        self.assertEqual(res.plan.actions[0].tool, "open_application")
        self.assertEqual(res.plan.actions[0].arguments["application"], "Google Chrome")
        # Action 2: browser_search("Python tutorials")
        self.assertEqual(res.plan.actions[1].tool, "browser_search")
        self.assertEqual(res.plan.actions[1].arguments["query"], "Python tutorials")
        self.assertFalse(mock_llm.create_plan.called)

    def test_146_test_b_two_applications(self):
        # Test B: "Open Chrome and then open Calculator"
        # Chrome first, Calculator second
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open Chrome and then open Calculator")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 2)
        self.assertEqual(res.plan.actions[0].tool, "open_application")
        self.assertEqual(res.plan.actions[0].arguments["application"], "Google Chrome")
        self.assertEqual(res.plan.actions[1].tool, "open_application")
        self.assertEqual(res.plan.actions[1].arguments["application"], "Calculator")
        self.assertFalse(mock_llm.create_plan.called)

    def test_147_test_c_two_folders(self):
        # Test C: "Open Downloads and then open Documents"
        # Downloads first, Documents second
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open Downloads and then open Documents")
        self.assertFalse(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 2)
        self.assertEqual(res.plan.actions[0].tool, "open_folder")
        self.assertEqual(res.plan.actions[0].arguments["folder"], "downloads")
        self.assertEqual(res.plan.actions[1].tool, "open_folder")
        self.assertEqual(res.plan.actions[1].arguments["folder"], "documents")
        self.assertFalse(mock_llm.create_plan.called)

    def test_148_test_d_search_query_containing_and(self):
        # Test D: "Search for AI and machine learning tutorials" and "Search for Python and Java tutorials"
        # ONE search action, query preserved, no multi-step split
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res1 = orchestrator.handle("Search for AI and machine learning tutorials")
        self.assertEqual(len(res1.plan.actions), 1)
        self.assertEqual(res1.plan.actions[0].tool, "browser_search")
        self.assertEqual(res1.plan.actions[0].arguments["query"], "AI and machine learning tutorials")

        res2 = orchestrator.handle("Search for Python and Java tutorials")
        self.assertEqual(len(res2.plan.actions), 1)
        self.assertEqual(res2.plan.actions[0].tool, "browser_search")
        self.assertEqual(res2.plan.actions[0].arguments["query"], "Python and Java tutorials")
        self.assertFalse(mock_llm.create_plan.called)

    def test_149_test_e_single_step_regression(self):
        # Test E: Verify existing single-step commands still produce one action
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res_app = orchestrator.handle("Open Chrome")
        self.assertEqual(len(res_app.plan.actions), 1)

        res_folder = orchestrator.handle("Open Downloads")
        self.assertEqual(len(res_folder.plan.actions), 1)

        res_search = orchestrator.handle("Search for Python tutorials")
        self.assertEqual(len(res_search.plan.actions), 1)

    def test_150_test_f_execution_order(self):
        # Test F: Mock or inspect executor calls and verify Action 1 -> Action 2 in exact order
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        execution_order = []
        original_execute_action = orchestrator.executor.execute_action

        def tracking_execute_action(action, allow_approved=False):
            execution_order.append(action.tool)
            return original_execute_action(action, allow_approved=allow_approved)

        orchestrator.executor.execute_action = tracking_execute_action

        res = orchestrator.handle("Open Downloads and then open Documents")
        self.assertEqual(execution_order, ["open_folder", "open_folder"])
        self.assertEqual(res.execution_result.results[0].arguments["folder"], "downloads")
        self.assertEqual(res.execution_result.results[1].arguments["folder"], "documents")

    def test_151_test_g_failure_handling(self):
        # Test G: If first action fails, stop subsequent actions and report clearly
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open SomeRandomUnknownApp and then open Calculator")
        self.assertIsNotNone(res.execution_result)
        self.assertEqual(len(res.execution_result.results), 2)
        # Result 1: Failed
        self.assertTrue(res.execution_result.results[0].executed)
        self.assertFalse(res.execution_result.results[0].success)
        self.assertEqual(res.execution_result.results[0].error, "UNKNOWN_APPLICATION")
        # Result 2: Not executed due to previous action failure
        self.assertFalse(res.execution_result.results[1].executed)
        self.assertFalse(res.execution_result.results[1].success)
        self.assertEqual(res.execution_result.results[1].error, "PREVIOUS_ACTION_FAILED")

    def test_152_test_h_maximum_action_limit(self):
        # Test H: Request containing >3 actions asks to split without partial execution
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        cmd = "Open Chrome and open Calculator and open Downloads and then open Documents"
        res = orchestrator.handle(cmd)
        self.assertTrue(res.plan.needs_clarification)
        self.assertEqual(len(res.plan.actions), 0)
        self.assertIn("maximum of 3 actions", res.message)
        self.assertFalse(mock_llm.create_plan.called)

    def test_153_test_i_file_context_regression(self):
        # Test I: Verify "Open my resume", "Open the updated one", "Open it again" still work
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        f1 = self.mock_documents / "Akhil_Resume(Updated).pdf"
        f2 = self.mock_documents / "MyResume.pdf"
        f1.write_text("resume1")
        f2.write_text("resume2")

        # Clarification
        r_clarif = orchestrator.handle("Open my resume")
        self.assertTrue(r_clarif.plan.needs_clarification)

        # Open updated one
        r_up = orchestrator.handle("Open the updated one")
        self.assertEqual(len(r_up.plan.actions), 1)
        self.assertEqual(r_up.plan.actions[0].tool, "open_file")
        self.assertEqual(r_up.plan.actions[0].arguments["path"], str(f1))

        # Open it again
        r_again = orchestrator.handle("Open it again")
        self.assertEqual(len(r_again.plan.actions), 1)
        self.assertEqual(r_again.plan.actions[0].tool, "open_file")
        self.assertEqual(r_again.plan.actions[0].arguments["path"], str(f1))

    # =========================================================================
    # Milestone 4D.3 - Fix Skipped Multi-Step Action Result Presentation Tests
    # =========================================================================

    def test_154_test_a_failed_first_action_stops_second_action(self):
        # Test A: "Open SomeRandomUnknownApp and then open Calculator"
        # First action fails, second action is marked skipped with PREVIOUS_ACTION_FAILED and does not execute
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open SomeRandomUnknownApp and then open Calculator")
        self.assertIsNotNone(res.execution_result)
        self.assertEqual(len(res.execution_result.results), 2)

        # Action 1
        r1 = res.execution_result.results[0]
        self.assertTrue(r1.executed)
        self.assertFalse(r1.success)
        self.assertEqual(r1.error, "UNKNOWN_APPLICATION")

        # Action 2
        r2 = res.execution_result.results[1]
        self.assertFalse(r2.executed)
        self.assertFalse(r2.success)
        self.assertEqual(r2.error, "PREVIOUS_ACTION_FAILED")
        self.assertIn("Action was not executed because the previous action failed", r2.message)

    def test_155_test_b_skipped_action_not_reported_as_unimplemented(self):
        # Test B: Verify presentation output for skipped action does NOT contain "not implemented yet"
        import io
        from main import main
        from unittest.mock import patch

        mock_input = iter(["Open SomeRandomUnknownApp and then open Calculator", "exit"])
        with patch("builtins.input", lambda prompt="": next(mock_input)), \
             patch("core.orchestrator.JarvisOrchestrator.check_connection"):
            with patch("sys.stdout", new_callable=io.StringIO) as fake_out:
                main()
                cli_output = fake_out.getvalue()

        self.assertIn("✗ open_application failed", cli_output)
        self.assertIn("⊘ open_application skipped", cli_output)
        self.assertIn("Action was not executed because the previous action failed", cli_output)
        self.assertNotIn("not implemented yet", cli_output)

    def test_156_test_c_three_step_failure(self):
        # Test C: "Open SomeRandomUnknownApp then open Calculator then open Downloads"
        # Action 1 fails, Action 2 & 3 skipped, neither executed, neither reported as unimplemented
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open SomeRandomUnknownApp then open Calculator then open Downloads")
        self.assertIsNotNone(res.execution_result)
        self.assertEqual(len(res.execution_result.results), 3)

        # Action 1: failed
        self.assertTrue(res.execution_result.results[0].executed)
        self.assertFalse(res.execution_result.results[0].success)
        self.assertEqual(res.execution_result.results[0].error, "UNKNOWN_APPLICATION")

        # Action 2: skipped
        self.assertFalse(res.execution_result.results[1].executed)
        self.assertEqual(res.execution_result.results[1].error, "PREVIOUS_ACTION_FAILED")

        # Action 3: skipped
        self.assertFalse(res.execution_result.results[2].executed)
        self.assertEqual(res.execution_result.results[2].error, "PREVIOUS_ACTION_FAILED")

        # Verify CLI output presentation
        import io
        from main import main
        from unittest.mock import patch

        mock_input = iter(["Open SomeRandomUnknownApp then open Calculator then open Downloads", "exit"])
        with patch("builtins.input", lambda prompt="": next(mock_input)), \
             patch("core.orchestrator.JarvisOrchestrator.check_connection"):
            with patch("sys.stdout", new_callable=io.StringIO) as fake_out:
                main()
                cli_output = fake_out.getvalue()

        self.assertIn("✗ open_application failed", cli_output)
        self.assertIn("⊘ open_application skipped", cli_output)
        self.assertIn("⊘ open_folder skipped", cli_output)
        self.assertNotIn("not implemented yet", cli_output)

    def test_157_test_d_genuine_success_regression(self):
        # Test D: "Open Chrome and then open Calculator" executes both actions successfully
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open Chrome and then open Calculator")
        self.assertIsNotNone(res.execution_result)
        self.assertEqual(len(res.execution_result.results), 2)
        self.assertTrue(res.execution_result.results[0].executed)
        self.assertTrue(res.execution_result.results[0].success)
        self.assertTrue(res.execution_result.results[1].executed)
        self.assertTrue(res.execution_result.results[1].success)

    def test_158_test_e_existing_failure_handling_regression(self):
        # Test E: "Open SomeRandomUnknownApp" single action still safely rejected
        mock_llm = MagicMock()
        orchestrator = JarvisOrchestrator(llm=mock_llm)

        res = orchestrator.handle("Open SomeRandomUnknownApp")
        self.assertIsNotNone(res.execution_result)
        self.assertEqual(len(res.execution_result.results), 1)
        self.assertTrue(res.execution_result.results[0].executed)
        self.assertFalse(res.execution_result.results[0].success)
        self.assertEqual(res.execution_result.results[0].error, "UNKNOWN_APPLICATION")


if __name__ == "__main__":
    unittest.main()
