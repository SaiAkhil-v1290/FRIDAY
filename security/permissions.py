"""
Central permission policy and structured approval workflow.
Deterministic enforcement:
- The LLM can propose actions; deterministic policy determines approval requirement.
- Approval requests are specific, time-limited, single-use, and bound to exact validated arguments.
- Unregistered tools are denied by default.
"""

from __future__ import annotations

import copy
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from config import settings
from tools.registry import TOOL_POLICIES, get_tool_policy, is_registered_tool


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    EXECUTED = "executed"
    FAILED = "failed"


@dataclass
class ApprovalRequest:
    """
    Formal, structured approval record.
    Bound to exact validated tool and arguments.
    """
    approval_id: str
    tool: str
    arguments: dict[str, Any]
    risk_level: str
    permission_category: str
    explanation: str
    created_at: float
    expires_at: float
    status: ApprovalStatus = ApprovalStatus.PENDING
    execution_status: str | None = None
    error: str | None = None

    def is_expired(self, current_time: float | None = None) -> bool:
        now = time.time() if current_time is None else current_time
        return now > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "tool": self.tool,
            "arguments": copy.deepcopy(self.arguments),
            "risk_level": self.risk_level,
            "permission_category": self.permission_category,
            "explanation": self.explanation,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "execution_status": self.execution_status,
            "error": self.error,
        }


class ApprovalManager:
    """
    Manages lifecycle of permission approval requests.
    Guarantees:
    - Unique approval ID (UUID).
    - Time-limited validity (expires after configured window).
    - Single-use: once validated and consumed for execution, transitions to EXECUTED.
    - Immutable arguments check: cannot execute with altered arguments.
    - Strictly prevents replay or duplicate execution.
    - Never allows the LLM to approve its own actions.
    """

    def __init__(self, default_timeout_seconds: float | None = None) -> None:
        self.default_timeout = default_timeout_seconds or getattr(settings, "approval_timeout_seconds", 300.0)
        self._requests: dict[str, ApprovalRequest] = {}
        self._audit_log: list[dict[str, Any]] = []

    def create_request(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        risk_level: str | None = None,
        permission_category: str | None = None,
        explanation: str = "",
        timeout_seconds: float | None = None,
    ) -> ApprovalRequest:
        if not is_registered_tool(tool):
            raise ValueError(f"Cannot create approval request for unregistered tool: '{tool}'.")

        policy = get_tool_policy(tool) or {}
        assigned_risk = risk_level or str(policy.get("risk_level", "high"))
        assigned_cat = permission_category or str(policy.get("category", "unclassified"))

        now = time.time()
        timeout = timeout_seconds if timeout_seconds is not None else self.default_timeout
        expires_at = now + timeout

        req_id = str(uuid.uuid4())
        canonical_args = copy.deepcopy(arguments) if arguments else {}

        req = ApprovalRequest(
            approval_id=req_id,
            tool=tool,
            arguments=canonical_args,
            risk_level=assigned_risk,
            permission_category=assigned_cat,
            explanation=explanation or f"Approval required to execute '{tool}'.",
            created_at=now,
            expires_at=expires_at,
            status=ApprovalStatus.PENDING,
        )

        self._requests[req_id] = req
        self._record_audit("CREATED", req)
        return req

    def get_request(self, approval_id: str) -> ApprovalRequest | None:
        req = self._requests.get(approval_id)
        if not req:
            return None
        # Auto-expire if pending and window has passed
        if req.status == ApprovalStatus.PENDING and req.is_expired():
            req.status = ApprovalStatus.EXPIRED
            self._record_audit("EXPIRED", req)
        return req

    def approve_request(self, approval_id: str) -> tuple[bool, str]:
        req = self.get_request(approval_id)
        if not req:
            return False, f"Approval request '{approval_id}' not found."

        if req.status == ApprovalStatus.EXPIRED or req.is_expired():
            req.status = ApprovalStatus.EXPIRED
            return False, "Approval request has expired and cannot be approved."

        if req.status == ApprovalStatus.REJECTED:
            return False, "Cannot approve a request that has been rejected."

        if req.status == ApprovalStatus.EXECUTED:
            return False, "Request has already been executed."

        if req.status == ApprovalStatus.APPROVED:
            return False, "Request is already approved."

        if req.status != ApprovalStatus.PENDING:
            return False, f"Cannot approve request in '{req.status.value}' state."

        req.status = ApprovalStatus.APPROVED
        self._record_audit("APPROVED", req)
        return True, f"Request '{approval_id}' for '{req.tool}' approved successfully."

    def reject_request(self, approval_id: str, reason: str = "") -> tuple[bool, str]:
        req = self.get_request(approval_id)
        if not req:
            return False, f"Approval request '{approval_id}' not found."

        if req.status == ApprovalStatus.EXECUTED:
            return False, "Cannot reject an action that has already been executed."

        req.status = ApprovalStatus.REJECTED
        req.error = reason or "Rejected by user."
        self._record_audit("REJECTED", req, reason=reason)
        return True, f"Request '{approval_id}' rejected."

    def validate_and_consume(
        self,
        approval_id: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """
        Atomically validate that approval is valid for the exact tool and arguments,
        and transition state to EXECUTED (single-use token protection).
        """
        req = self.get_request(approval_id)
        if not req:
            return False, f"Approval request '{approval_id}' does not exist."

        if req.status == ApprovalStatus.EXPIRED or req.is_expired():
            req.status = ApprovalStatus.EXPIRED
            return False, "Approval request has expired."

        if req.status == ApprovalStatus.REJECTED:
            return False, "Approval request was explicitly rejected."

        if req.status == ApprovalStatus.EXECUTED:
            return False, "Approval request has already been executed (replay attack blocked)."

        if req.status == ApprovalStatus.PENDING:
            return False, "Action requires explicit user approval before execution."

        if req.status != ApprovalStatus.APPROVED:
            return False, f"Invalid approval status: '{req.status.value}'."

        # Verify tool identity
        if req.tool != tool:
            return False, (
                f"Approval tool mismatch: approved for '{req.tool}', "
                f"attempted execution of '{tool}'."
            )

        # Verify exact arguments (tampering protection)
        candidate_args = copy.deepcopy(arguments) if arguments else {}
        if req.arguments != candidate_args:
            return False, (
                "Approval arguments mismatch: execution arguments do not match "
                "the approved request (tampering prevented)."
            )

        # Transition to EXECUTED - prevents any second execution
        req.status = ApprovalStatus.EXECUTED
        req.execution_status = "executing"
        self._record_audit("CONSUMED_FOR_EXECUTION", req)
        return True, None

    def mark_failed(self, approval_id: str, error_message: str) -> None:
        req = self._requests.get(approval_id)
        if req:
            req.status = ApprovalStatus.FAILED
            req.execution_status = "failed"
            req.error = error_message
            self._record_audit("EXECUTION_FAILED", req, error=error_message)

    def mark_completed(self, approval_id: str) -> None:
        req = self._requests.get(approval_id)
        if req:
            req.execution_status = "completed"
            self._record_audit("EXECUTION_COMPLETED", req)

    def list_requests(self, status: ApprovalStatus | None = None) -> list[ApprovalRequest]:
        now = time.time()
        for req in self._requests.values():
            if req.status == ApprovalStatus.PENDING and req.is_expired(now):
                req.status = ApprovalStatus.EXPIRED
        if status is not None:
            return [r for r in self._requests.values() if r.status == status]
        return list(self._requests.values())

    def get_audit_log(self) -> list[dict[str, Any]]:
        return list(self._audit_log)

    def _record_audit(self, event_type: str, req: ApprovalRequest, **extra: Any) -> None:
        entry = {
            "timestamp": time.time(),
            "event": event_type,
            "approval_id": req.approval_id,
            "tool": req.tool,
            "risk_level": req.risk_level,
            "status": req.status.value,
        }
        entry.update(extra)
        self._audit_log.append(entry)


# Global approval manager instance
approval_manager = ApprovalManager()


def requires_approval(tool_name: str) -> bool:
    """Check if tool requires human approval. Raises ValueError for unknown tools."""
    policy = TOOL_POLICIES.get(tool_name)
    if policy is None:
        raise ValueError(f"Unknown tool: {tool_name}")
    return bool(policy.get("requires_approval", False))


def get_tool_risk(tool_name: str) -> str:
    """Get risk level for tool ('low', 'medium', 'high', 'critical'). Unknown tools are 'critical'."""
    policy = TOOL_POLICIES.get(tool_name)
    if policy is None:
        return "critical"
    return str(policy.get("risk_level", "high"))


def get_tool_category(tool_name: str) -> str:
    """Get category for tool. Unknown tools return 'unknown'."""
    policy = TOOL_POLICIES.get(tool_name)
    if policy is None:
        return "unknown"
    return str(policy.get("category", "unknown"))
