import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from config import Settings, ConfigurationError, load_settings
from ai.schemas import (
    ProposedAction,
    TaskPlan,
    validate_action_arguments,
    OpenApplicationArgs,
    BrowserSearchArgs,
)
from pydantic import ValidationError
from security.permissions import (
    ApprovalManager,
    ApprovalRequest,
    ApprovalStatus,
    requires_approval,
    get_tool_risk,
    get_tool_category,
)
from security.audit import AuditLogger, audit_logger
from core.executor import PlanExecutor, ActionExecutionResult
from core.planner import validate_plan
from core.orchestrator import JarvisOrchestrator
from tools.files import (
    SAFE_SEARCH_ROOTS,
    open_local_file,
    reveal_local_file,
    open_local_folder,
    discover_files,
    search_file_contents,
    extract_searchable_units,
    extract_text_file_units,
    extract_pdf_units,
)
from tools.application import (
    resolve_application,
    launch_application,
    close_application,
    terminate_process_by_pid,
)


class TestPhase1Hardening(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="jarvis_phase1_test_")).resolve()
        self.mock_desktop = self.test_dir / "Desktop"
        self.mock_documents = self.test_dir / "Documents"
        self.mock_downloads = self.test_dir / "Downloads"
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

    def tearDown(self):
        self.roots_patcher.stop()
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    # =========================================================================
    # TASK 3: CONFIGURATION VALIDATION TESTS
    # =========================================================================
    def test_config_valid_loading(self):
        env = {
            "OLLAMA_HOST": "http://127.0.0.1:11434",
            "OLLAMA_MODEL": "qwen2.5:3b",
            "OLLAMA_TIMEOUT": "60",
            "MAX_HISTORY_MESSAGES": "20",
            "MAX_DOCUMENT_SIZE_BYTES": "5242880",
            "APPROVAL_TIMEOUT_SECONDS": "180",
        }
        s = load_settings(env)
        self.assertEqual(s.ollama_host, "http://127.0.0.1:11434")
        self.assertEqual(s.ollama_model, "qwen2.5:3b")
        self.assertEqual(s.request_timeout, 60.0)
        self.assertEqual(s.max_history_messages, 20)
        self.assertEqual(s.max_document_size_bytes, 5242880)
        self.assertEqual(s.approval_timeout_seconds, 180.0)

    def test_config_missing_uses_defaults(self):
        s = load_settings({})
        self.assertEqual(s.ollama_host, "http://127.0.0.1:11434")
        self.assertEqual(s.ollama_model, "qwen2.5:3b")
        self.assertEqual(s.request_timeout, 120.0)
        self.assertEqual(s.max_history_messages, 12)
        self.assertEqual(s.max_document_size_bytes, 10 * 1024 * 1024)

    def test_config_invalid_url_scheme_rejected(self):
        with self.assertRaises(ConfigurationError):
            load_settings({"OLLAMA_HOST": "ftp://127.0.0.1:11434"})

    def test_config_empty_url_rejected(self):
        with self.assertRaises(ConfigurationError):
            load_settings({"OLLAMA_HOST": ""})

    def test_config_malformed_float_rejected(self):
        with self.assertRaises(ConfigurationError):
            load_settings({"OLLAMA_TIMEOUT": "not-a-number"})

    def test_config_negative_number_rejected(self):
        with self.assertRaises(ConfigurationError):
            load_settings({"OLLAMA_TIMEOUT": "-10"})

    def test_config_out_of_bounds_int_rejected(self):
        with self.assertRaises(ConfigurationError):
            load_settings({"MAX_HISTORY_MESSAGES": "0"})

    def test_config_illegal_characters_in_model_rejected(self):
        with self.assertRaises(ConfigurationError):
            load_settings({"OLLAMA_MODEL": "qwen; rm -rf /"})

    # =========================================================================
    # TASK 2: INTENT ROUTING & SCHEMA VALIDATION TESTS
    # =========================================================================
    def test_schema_valid_action(self):
        action = ProposedAction(
            tool="open_application",
            arguments={"application": "notepad"},
            requires_approval=False,
            reason="User asked to open Notepad",
        )
        self.assertEqual(action.tool, "open_application")
        self.assertEqual(action.arguments["application"], "notepad")

    def test_schema_forbids_unknown_fields(self):
        with self.assertRaises(ValidationError):
            ProposedAction(
                tool="open_application",
                arguments={"application": "notepad"},
                requires_approval=False,
                reason="test",
                malicious_field="injection",  # extra forbidden field
            )

    def test_schema_empty_tool_name_rejected(self):
        with self.assertRaises(ValidationError):
            ProposedAction(
                tool="   ",
                arguments={},
                requires_approval=False,
                reason="test",
            )

    def test_schema_arguments_must_be_dict(self):
        with self.assertRaises(ValidationError):
            ProposedAction(
                tool="open_application",
                arguments=["not_a_dict"],
                requires_approval=False,
                reason="test",
            )

    def test_argument_validation_for_registered_tools(self):
        valid, err = validate_action_arguments("open_application", {"application": "chrome"})
        self.assertTrue(valid)
        self.assertIsNone(err)

        # Missing required application field
        valid_bad, err_bad = validate_action_arguments("open_application", {})
        self.assertFalse(valid_bad)
        self.assertIsNotNone(err_bad)

        # Extra forbidden field in tool arguments
        valid_extra, err_extra = validate_action_arguments("open_application", {"application": "chrome", "hacked": True})
        self.assertFalse(valid_extra)
        self.assertIsNotNone(err_extra)

    # =========================================================================
    # TASK 4: APPROVAL WORKFLOW TESTS
    # =========================================================================
    def test_approval_manager_create_and_approve(self):
        mgr = ApprovalManager(default_timeout_seconds=300)
        req = mgr.create_request("delete_files", {"folder": "Downloads"}, risk_level="critical")
        self.assertEqual(req.status, ApprovalStatus.PENDING)
        self.assertEqual(req.tool, "delete_files")
        self.assertIsNotNone(req.approval_id)

        ok, msg = mgr.approve_request(req.approval_id)
        self.assertTrue(ok)
        self.assertEqual(req.status, ApprovalStatus.APPROVED)

    def test_approval_manager_reject(self):
        mgr = ApprovalManager(default_timeout_seconds=300)
        req = mgr.create_request("delete_files", {"folder": "Downloads"})
        ok, msg = mgr.reject_request(req.approval_id, reason="User cancelled")
        self.assertTrue(ok)
        self.assertEqual(req.status, ApprovalStatus.REJECTED)

        # Cannot approve rejected request
        ok2, msg2 = mgr.approve_request(req.approval_id)
        self.assertFalse(ok2)
        self.assertIn("rejected", msg2)

    def test_approval_manager_expiration(self):
        # 1-second timeout
        mgr = ApprovalManager(default_timeout_seconds=0.01)
        req = mgr.create_request("delete_files", {"folder": "Downloads"})
        time.sleep(0.05)
        # Should now evaluate to expired
        ok, msg = mgr.approve_request(req.approval_id)
        self.assertFalse(ok)
        self.assertIn("expired", msg)
        self.assertEqual(req.status, ApprovalStatus.EXPIRED)

    def test_approval_manager_single_use_consumption(self):
        mgr = ApprovalManager()
        req = mgr.create_request("delete_files", {"folder": "Downloads"})
        mgr.approve_request(req.approval_id)

        # First consumption: valid
        valid, err = mgr.validate_and_consume(req.approval_id, "delete_files", {"folder": "Downloads"})
        self.assertTrue(valid)
        self.assertIsNone(err)
        self.assertEqual(req.status, ApprovalStatus.EXECUTED)

        # Second consumption: blocked as replay attack!
        valid2, err2 = mgr.validate_and_consume(req.approval_id, "delete_files", {"folder": "Downloads"})
        self.assertFalse(valid2)
        self.assertIn("already been executed", err2)

    def test_approval_manager_tool_mismatch_blocked(self):
        mgr = ApprovalManager()
        req = mgr.create_request("delete_files", {"folder": "Downloads"})
        mgr.approve_request(req.approval_id)

        # Attempt to use approval for different tool
        valid, err = mgr.validate_and_consume(req.approval_id, "open_application", {"application": "calc"})
        self.assertFalse(valid)
        self.assertIn("mismatch", err)

    def test_approval_manager_argument_tampering_blocked(self):
        mgr = ApprovalManager()
        req = mgr.create_request("delete_files", {"folder": "Downloads"})
        mgr.approve_request(req.approval_id)

        # Attempt to execute with altered arguments
        valid, err = mgr.validate_and_consume(req.approval_id, "delete_files", {"folder": "Desktop"})
        self.assertFalse(valid)
        self.assertIn("mismatch", err)

    # =========================================================================
    # TASK 5: EXECUTION-LEVEL POLICY ENFORCEMENT TESTS
    # =========================================================================
    def test_executor_denies_unregistered_tool(self):
        executor = PlanExecutor()
        action = ProposedAction(
            tool="malicious_unregistered_tool",
            arguments={},
            requires_approval=False,
            reason="exploit",
        )
        res = executor.execute_action(action)
        self.assertFalse(res.executed)
        self.assertEqual(res.error, "UNREGISTERED_TOOL")

    def test_executor_denies_invalid_arguments(self):
        executor = PlanExecutor()
        action = ProposedAction(
            tool="browser_search",
            arguments={},  # missing required query
            requires_approval=False,
            reason="search",
        )
        res = executor.execute_action(action)
        self.assertFalse(res.executed)
        self.assertEqual(res.error, "INVALID_ARGUMENTS")

    def test_executor_enforces_structured_approval(self):
        executor = PlanExecutor()
        mgr = ApprovalManager()
        req = mgr.create_request("delete_files", {"folder": "Downloads", "pattern": "*"})

        action = ProposedAction(
            tool="delete_files",
            arguments={"folder": "Downloads", "pattern": "*"},
            requires_approval=True,
            reason="delete",
            approval_id=req.approval_id,
        )

        # With pending approval: held
        with patch("core.executor.approval_manager", mgr):
            res_pending = executor.execute_action(action)
            self.assertFalse(res_pending.executed)
            self.assertEqual(res_pending.error, "APPROVAL_INVALID")

            # Approve the request
            mgr.approve_request(req.approval_id)

            # Now execute: passes approval check (and hits EXECUTION_NOT_IMPLEMENTED for delete_files safely)
            res_approved = executor.execute_action(action)
            self.assertFalse(res_approved.executed)
            self.assertEqual(res_approved.error, "EXECUTION_NOT_IMPLEMENTED")

    # =========================================================================
    # TASK 6: FILE ACCESS SECURITY HARDENING TESTS
    # =========================================================================
    def test_open_file_path_traversal_blocked(self):
        traversal_path = "../../Windows/System32/drivers/etc/hosts"
        res = open_local_file(traversal_path)
        self.assertFalse(res.success)
        self.assertEqual(res.error, "UNSAFE_LOCATION")

    def test_open_file_outside_safe_roots_blocked(self):
        windows_path = "C:/Windows/notepad.exe"
        res = open_local_file(windows_path)
        self.assertFalse(res.success)
        self.assertEqual(res.error, "UNSAFE_LOCATION")

    def test_open_file_nonexistent_returns_file_not_found(self):
        fake_file = self.mock_downloads / "nonexistent.txt"
        res = open_local_file(str(fake_file))
        self.assertFalse(res.success)
        self.assertEqual(res.error, "FILE_NOT_FOUND")

    def test_open_file_directory_returns_not_a_file(self):
        res = open_local_file(str(self.mock_downloads))
        self.assertFalse(res.success)
        self.assertEqual(res.error, "NOT_A_FILE")

    def test_open_folder_outside_roots_blocked(self):
        res = open_local_folder("C:/Windows/System32")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "UNSAFE_LOCATION")

    def test_open_folder_safe_root_succeeds(self):
        with patch("os.startfile", create=True) as mock_start:
            res = open_local_folder("downloads")
            self.assertTrue(res.success)
            self.assertEqual(res.folder, "downloads")

    # =========================================================================
    # TASK 7: DOCUMENT EXTRACTION RESOURCE LIMITS TESTS
    # =========================================================================
    def test_extraction_resource_limits_text_file(self):
        # Create a text file with 100 lines
        test_file = self.mock_documents / "large_text.txt"
        content = "\n".join([f"Line number {i} with some content" for i in range(1, 101)])
        test_file.write_text(content, encoding="utf-8")

        # Test bounded extraction with max_units=5
        units = list(extract_text_file_units(test_file, max_chars=10000, max_units=5))
        self.assertEqual(len(units), 5)
        self.assertEqual(units[0].location, "Line 1")
        self.assertEqual(units[4].location, "Line 5")

    def test_extraction_handles_corrupted_pdf_gracefully(self):
        bad_pdf = self.mock_downloads / "corrupted.pdf"
        bad_pdf.write_bytes(b"garbage content not a valid pdf header")

        # Must not raise an unhandled exception or crash
        units = list(extract_pdf_units(bad_pdf))
        self.assertEqual(len(units), 0)

    def test_extraction_skips_oversized_file(self):
        huge_file = self.mock_downloads / "huge.txt"
        huge_file.write_text("Hello world", encoding="utf-8")

        # Patch settings with a small max_document_size_bytes
        custom_settings = Settings(
            ollama_host="http://127.0.0.1:11434",
            ollama_model="qwen2.5:3b",
            request_timeout=120.0,
            max_history_messages=12,
            max_document_size_bytes=5,
        )
        with patch("config.settings", custom_settings):
            units = list(extract_searchable_units(huge_file, ".txt"))
            self.assertEqual(len(units), 0)

    # =========================================================================
    # TASK 8: PROCESS TERMINATION SAFETY TESTS
    # =========================================================================
    def test_close_unknown_application_rejected(self):
        res = close_application("malicious_hacker_app")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "UNKNOWN_APPLICATION")

    def test_close_protected_application_rejected(self):
        res = close_application("explorer")
        self.assertFalse(res.success)
        self.assertEqual(res.error, "APPLICATION_NOT_CLOSABLE")

    def test_terminate_process_by_pid_rejects_system_pids(self):
        # PID 0, 1, 4 are system/idle processes
        self.assertFalse(terminate_process_by_pid(0))
        self.assertFalse(terminate_process_by_pid(4))

    # =========================================================================
    # TASK 9: AUDIT LOGGING TESTS
    # =========================================================================
    def test_audit_logger_redacts_passwords_and_tokens(self):
        logger = AuditLogger()
        logger.log(
            "TEST_EVENT",
            "browser_navigate",
            "SUCCESS",
            password="super_secret_password",
            auth_token="secret_token_123",
            user="admin",
        )
        events = logger.get_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].details["password"], "[REDACTED]")
        self.assertEqual(events[0].details["auth_token"], "[REDACTED]")
        self.assertEqual(events[0].details["user"], "admin")

    # =========================================================================
    # TASK 1 & 4 END-TO-END ORCHESTRATOR APPROVAL FLOW
    # =========================================================================
    def test_orchestrator_approval_and_rejection_workflow(self):
        # Create an orchestrator with a mock LLM returning a base TaskPlan
        mock_llm = MagicMock()
        mock_llm.create_plan.return_value = TaskPlan(
            intent="task",
            message="Plan to delete",
            actions=[],
            risk_level="low",
        )
        orch = JarvisOrchestrator(mock_llm)

        # Plan requiring approval (delete files)
        result = orch.handle("delete all files in downloads")
        self.assertEqual(result.plan.risk_level, "high")
        self.assertTrue(result.plan.actions[0].requires_approval)
        app_id = result.plan.actions[0].approval_id
        self.assertIsNotNone(app_id)

        # Test rejection command
        res_reject = orch.handle(f"reject {app_id}")
        self.assertIn("rejected", res_reject.message.lower())

        # Create a second approval request to test approval
        req = orch.handle("delete all files in downloads")
        app_id_2 = req.plan.actions[0].approval_id

        # Test approve command
        res_approve = orch.handle(f"approve {app_id_2}")
        self.assertIn("approved", res_approve.message.lower())
        # delete_files execution is safely not implemented yet, so execution is held
        self.assertIsNotNone(res_approve.execution_result)


if __name__ == "__main__":
    unittest.main()
