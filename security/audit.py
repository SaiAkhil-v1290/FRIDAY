"""
Audit trail for security events, permission checks, approval decisions,
and tool executions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class AuditEvent:
    timestamp: float
    event_type: str
    tool: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "event_type": self.event_type,
            "tool": self.tool,
            "status": self.status,
            "details": self.details,
        }


class AuditLogger:
    """
    In-memory structured audit logger for security-relevant operations.
    Preserves diagnostic context without exposing credentials or document secrets.
    """

    def __init__(self, max_entries: int = 2000) -> None:
        self.max_entries = max_entries
        self._events: list[AuditEvent] = []

    def log(
        self,
        event_type: str,
        tool: str,
        status: str,
        **details: Any,
    ) -> AuditEvent:
        # Sanitize details to prevent logging potential secrets
        sanitized_details = {}
        for k, v in details.items():
            if any(secret_kw in k.lower() for secret_kw in ["password", "secret", "token", "key", "cred"]):
                sanitized_details[k] = "[REDACTED]"
            elif isinstance(v, str) and len(v) > 200:
                sanitized_details[k] = v[:200] + "... [truncated]"
            else:
                sanitized_details[k] = v

        event = AuditEvent(
            timestamp=time.time(),
            event_type=event_type,
            tool=tool,
            status=status,
            details=sanitized_details,
        )
        self._events.append(event)
        if len(self._events) > self.max_entries:
            self._events = self._events[-self.max_entries:]
        return event

    def get_events(
        self,
        event_type: str | None = None,
        tool: str | None = None,
        limit: int | None = None,
    ) -> list[AuditEvent]:
        filtered = self._events
        if event_type is not None:
            filtered = [e for e in filtered if e.event_type == event_type]
        if tool is not None:
            filtered = [e for e in filtered if e.tool == tool]
        if limit is not None:
            return filtered[-limit:]
        return list(filtered)

    def clear(self) -> None:
        self._events.clear()


audit_logger = AuditLogger()

