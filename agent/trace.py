"""Decision Trace logging module for Exam Crisis Desk.

Records every:
- tool call (arguments, result, reason, call number)
- agent decision and reasoning
- candidate evaluation and rejection reason
- verification conflict
- approval request and response
- irreversible action
- post-action verification
- budget status and termination reason

Saves the audit log to logs/decision_trace.json.
"""

from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


class DecisionTrace:
    """Manages the structured audit trail of agent operations and decisions."""

    def __init__(self, trace_file: str = "logs/decision_trace.json") -> None:
        self.trace_file = trace_file
        self.events: List[Dict[str, Any]] = []
        self.step_counter: int = 0
        self.metadata: Dict[str, Any] = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "exam_id": "CSE476",
            "final_run_mode": os.getenv("FINAL_RUN", "false").lower() == "true",
            "max_tool_budget": 40,
            "max_iteration_budget": 15
        }

    def reset(self) -> None:
        """Reset the trace events and counter."""
        self.events = []
        self.step_counter = 0
        self.metadata["created_at"] = datetime.now(timezone.utc).isoformat()
        self.metadata["final_run_mode"] = os.getenv("FINAL_RUN", "false").lower() == "true"

    def _next_step(self) -> int:
        self.step_counter += 1
        return self.step_counter

    def log_event(self, event_type: str, reason: str, decision: str = "continue", **extra: Any) -> Dict[str, Any]:
        """Record an arbitrary structured event into the audit trail."""
        step = self._next_step()
        entry: Dict[str, Any] = {
            "step": step,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": event_type,
            "reason": reason,
            "decision": decision
        }
        entry.update(extra)
        self.events.append(entry)
        return entry

    def log_tool_call(
        self,
        tool: str,
        arguments: Dict[str, Any],
        call_number: int,
        success: bool,
        result: Optional[Dict[str, Any]] = None,
        error: Optional[str] = None,
        reason: str = ""
    ) -> Dict[str, Any]:
        """Record a tool invocation through the centralized wrapper."""
        return self.log_event(
            event_type="tool_call" if success else "tool_error",
            reason=reason or f"Executing tool {tool}",
            decision="continue" if success else "handle_tool_failure",
            tool=tool,
            call_number=call_number,
            arguments=arguments,
            success=success,
            result=result,
            error=error
        )

    def log_decision(self, reason: str, decision: str, **extra: Any) -> Dict[str, Any]:
        """Record a strategic or tactical reasoning decision."""
        return self.log_event(
            event_type="decision",
            reason=reason,
            decision=decision,
            **extra
        )

    def log_candidate_evaluation(
        self,
        candidate: Dict[str, Any],
        valid: bool,
        reasons: List[str],
        decision: str
    ) -> Dict[str, Any]:
        """Record candidate evaluation outcome and why it was approved or rejected."""
        return self.log_event(
            event_type="candidate_evaluation",
            reason="; ".join(reasons) if reasons else ("Candidate meets all verified constraints" if valid else "Rejected"),
            decision=decision,
            candidate=candidate,
            valid=valid,
            rejection_reasons=reasons if not valid else []
        )

    def log_verification_conflict(self, reason: str, conflict_details: Dict[str, Any]) -> Dict[str, Any]:
        """Record a tool output contradiction or conflicting evidence."""
        return self.log_event(
            event_type="verification_conflict",
            reason=reason,
            decision="reject_and_choose_safer_option",
            conflict_details=conflict_details
        )

    def log_approval(self, summary: str, approved: bool, feedback: str) -> Dict[str, Any]:
        """Record human approval gate query and response."""
        return self.log_event(
            event_type="approval",
            reason=f"Human authorization gate for irreversible actions: {'APPROVED' if approved else 'REJECTED'}",
            decision="proceed_with_execution" if approved else "replan_after_rejection",
            summary=summary,
            approved=approved,
            feedback=feedback
        )

    def log_final_result(self, status: str, final_summary: Dict[str, Any]) -> Dict[str, Any]:
        """Record the final outcome of the agent execution."""
        return self.log_event(
            event_type="final_result",
            reason=f"Agent terminated with status: {status}",
            decision=status,
            final_summary=final_summary
        )

    def save(self, output_path: Optional[str] = None) -> str:
        """Write the trace to disk in JSON format."""
        target_path = output_path or self.trace_file
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        payload = {
            "metadata": self.metadata,
            "total_steps": len(self.events),
            "events": self.events
        }
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        return target_path


# Global singleton instance
trace_instance = DecisionTrace()
