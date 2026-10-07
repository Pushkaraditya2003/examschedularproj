"""Safety Gate and Policy Enforcement Module.

Enforces:
1. Strict Task Scope Control (authorized ONLY for CSE476 exam rescheduling).
2. Human Approval Gate for irreversible actions (book_room, reschedule_exam, send_notice).
3. Untrusted Data Inspection and Prompt Injection Defense.
4. Hard Agent Iteration Limit (MAX_AGENT_ITERATIONS = 15).
5. FINAL_RUN freeze validation.
"""

from __future__ import annotations
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from agent.trace import trace_instance

MAX_AGENT_ITERATIONS: int = 15
ALLOWED_COURSE: str = "CSE476"
IRREVERSIBLE_ACTIONS: Set[str] = {"book_room", "reschedule_exam", "send_notice"}

SUSPICIOUS_INJECTION_PATTERNS = [
    r"ignore (?:all )?(?:previous |other |safety )*(?:rules|checks|instructions|constraints|safety)",
    r"bypass (?:approval|verification|checks|safety)",
    r"(?:dean|instructor|chair) (?:already )?approved",
    r"execute (?:the change |immediately|without checks)",
    r"(?:move|reschedule) .* to saturday",
    r"override (?:security|system|safety)",
    r"reveal (?:api[ _-]?key|secret|credentials|password)",
    r"system prompt",
]


class SafetyGate:
    """Enforces non-bypassable deterministic safety rules."""

    def __init__(self, max_iterations: int = MAX_AGENT_ITERATIONS) -> None:
        self.max_iterations = max_iterations
        self.iteration_count: int = 0
        self.approved_candidates: Set[str] = set()  # set of candidate keys that were human-approved
        self.rejected_candidates: Set[str] = set()

    def reset(self) -> None:
        """Reset iteration counter and candidate approvals."""
        self.iteration_count = 0
        self.approved_candidates.clear()
        self.rejected_candidates.clear()

    def is_final_run(self) -> bool:
        """Check if environment has locked final evaluation mode."""
        return os.getenv("FINAL_RUN", "false").lower() == "true"

    def increment_iteration(self) -> Tuple[bool, int, str]:
        """Track agent iterations and stop safely at 15."""
        self.iteration_count += 1
        if self.iteration_count > self.max_iterations:
            msg = (
                f"HARD ITERATION LIMIT REACHED: Agent reached {self.iteration_count} iterations "
                f"(max {self.max_iterations}). Stopping safely to prevent infinite loops."
            )
            trace_instance.log_event(
                event_type="iteration_limit_exceeded",
                reason=msg,
                decision="terminate_agent_safely"
            )
            return False, self.iteration_count, msg
        return True, self.iteration_count, f"Iteration {self.iteration_count}/{self.max_iterations}"

    def validate_scope(self, target_identifier: str, action: str) -> Tuple[bool, str]:
        """Enforce task boundary: strictly restricted to CSE476 and exam operations."""
        # Clean target identifier
        target = target_identifier.strip().upper()
        if target != ALLOWED_COURSE:
            msg = (
                f"SCOPE VIOLATION: Requested target '{target_identifier}' is outside authorized scope. "
                f"Agent is restricted strictly to '{ALLOWED_COURSE}'."
            )
            trace_instance.log_event(
                event_type="scope_violation",
                reason=msg,
                decision="reject_out_of_scope_action",
                target=target_identifier,
                action=action
            )
            return False, msg

        allowed_actions = {
            "inspect", "plan", "verify", "book_room", "reschedule_exam", "send_notice",
            "read_inbox", "get_exam", "list_rooms", "get_room_bookings", "get_faculty_availability",
            "get_enrolled_students", "get_student_schedule", "request_human_approval"
        }
        if action not in allowed_actions:
            msg = f"SCOPE VIOLATION: Action '{action}' is outside authorized exam rescheduling operations."
            trace_instance.log_event(
                event_type="scope_violation",
                reason=msg,
                decision="reject_out_of_scope_action",
                target=target_identifier,
                action=action
            )
            return False, msg

        return True, f"Action '{action}' on '{ALLOWED_COURSE}' is within authorized scope."

    def inspect_untrusted_data(self, source: str, content: str) -> Tuple[bool, Optional[str]]:
        """Inspect untrusted inputs (inbox messages, tool outputs) for prompt injection attempts.
        
        Returns: (is_safe, detected_pattern_description)
        """
        for pat in SUSPICIOUS_INJECTION_PATTERNS:
            if re.search(pat, content, re.IGNORECASE):
                reason = f"PROMPT INJECTION ATTEMPT DETECTED in {source}: matches pattern '{pat}'."
                trace_instance.log_event(
                    event_type="prompt_injection_flagged",
                    reason=reason,
                    decision="treat_strictly_as_untrusted_data_and_ignore_embedded_instructions",
                    source=source,
                    snippet=content[:200]
                )
                return False, reason

        return True, None

    def assert_approval_before_irreversible(
        self,
        action: str,
        candidate_key: str,
        is_candidate_verified: bool,
        is_approval_granted: bool
    ) -> Tuple[bool, str]:
        """Verify that an irreversible action satisfies both verification and human approval gates."""
        if action not in IRREVERSIBLE_ACTIONS:
            return True, f"Action '{action}' is read-only or reversible."

        if not is_candidate_verified:
            err = (
                f"SAFETY GATE BLOCKED: Irreversible action '{action}' rejected because proposed "
                f"candidate '{candidate_key}' has NOT been deterministically verified."
            )
            trace_instance.log_event(
                event_type="safety_gate_blocked",
                reason=err,
                decision="abort_irreversible_action"
            )
            return False, err

        if not is_approval_granted or candidate_key not in self.approved_candidates:
            err = (
                f"SAFETY GATE BLOCKED: Irreversible action '{action}' requires explicit human "
                f"approval for candidate '{candidate_key}' before execution."
            )
            trace_instance.log_event(
                event_type="safety_gate_blocked",
                reason=err,
                decision="abort_irreversible_action"
            )
            return False, err

        return True, f"Safety gate passed: candidate '{candidate_key}' is verified and human-approved."

    def record_human_approval(self, candidate_key: str, approved: bool, feedback: str = "") -> None:
        """Record the human decision for a specific candidate schedule."""
        if approved:
            self.approved_candidates.add(candidate_key)
            if candidate_key in self.rejected_candidates:
                self.rejected_candidates.remove(candidate_key)
        else:
            self.rejected_candidates.add(candidate_key)
            if candidate_key in self.approved_candidates:
                self.approved_candidates.remove(candidate_key)

        trace_instance.log_approval(
            summary=f"Candidate: {candidate_key}",
            approved=approved,
            feedback=feedback
        )

    def is_candidate_rejected(self, candidate_key: str) -> bool:
        """Check if this candidate was previously rejected by the human."""
        return candidate_key in self.rejected_candidates


# Global safety gate instance
safety_gate_instance = SafetyGate()
