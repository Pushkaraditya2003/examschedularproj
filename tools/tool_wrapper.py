"""Centralized Tool Execution Wrapper.

Enforces:
- Hard 40-call budget limit
- Strict tool name and argument validation
- Structured error handling without unhandled exceptions
- Automatic logging into Decision Trace
- Rejection of direct ERP access
"""

from __future__ import annotations
from typing import Any, Callable, Dict, Optional, Set
import inspect

from agent.trace import trace_instance
from tools import erp

MAX_TOOL_CALLS: int = 40


class ToolWrapper:
    """Centralized controller for ERP tool invocations with strict budget tracking."""

    def __init__(self, max_calls: int = MAX_TOOL_CALLS) -> None:
        self.max_calls = max_calls
        self.call_count: int = 0
        self.registry: Dict[str, Callable[..., Dict[str, Any]]] = {}
        self._register_tools()

    def reset(self) -> None:
        """Reset the call counter."""
        self.call_count = 0

    def _register_tools(self) -> None:
        self.registry = {
            "get_exam": erp.get_exam,
            "list_rooms": erp.list_rooms,
            "get_room_bookings": erp.get_room_bookings,
            "get_faculty_availability": erp.get_faculty_availability,
            "get_enrolled_students": erp.get_enrolled_students,
            "get_student_schedule": erp.get_student_schedule,
            "read_inbox": erp.read_inbox,
            "book_room": erp.book_room,
            "reschedule_exam": erp.reschedule_exam,
            "send_notice": erp.send_notice,
            "request_human_approval": erp.request_human_approval,
        }

    def _validate_arguments(self, func: Callable[..., Any], args: Dict[str, Any]) -> Optional[str]:
        """Validate passed arguments against function signature."""
        sig = inspect.signature(func)
        bound_args = {}
        for param_name, param in sig.parameters.items():
            if param.default is inspect.Parameter.empty:
                if param_name not in args:
                    return f"Missing required parameter '{param_name}'."
            if param_name in args:
                bound_args[param_name] = args[param_name]
        
        # Check for unexpected extra arguments
        unexpected = set(args.keys()) - set(sig.parameters.keys())
        if unexpected:
            return f"Unexpected parameters provided: {list(unexpected)}."
            
        return None

    def call_tool(
        self,
        tool_name: str,
        arguments: Optional[Dict[str, Any]] = None,
        reason: str = ""
    ) -> Dict[str, Any]:
        """Central gatekeeper for all tool calls.
        
        Returns structured dictionary:
        {
            "success": bool,
            "tool": str,
            "call_number": int,
            "result": dict (if success),
            "error": str (if failure)
        }
        """
        args = arguments or {}

        # 1. Validate tool name
        if tool_name not in self.registry:
            err_msg = f"Unknown or disallowed tool: '{tool_name}'."
            trace_instance.log_event(
                event_type="security_or_validation_violation",
                reason=err_msg,
                decision="reject_call",
                tool=tool_name,
                arguments=args
            )
            return {
                "success": False,
                "tool": tool_name,
                "call_number": self.call_count,
                "error": err_msg
            }

        # 2. Check hard tool-call budget
        if self.call_count >= self.max_calls:
            err_msg = (
                f"HARD BUDGET EXHAUSTED: Tool call '{tool_name}' blocked because maximum "
                f"budget of {self.max_calls} calls has been reached."
            )
            trace_instance.log_event(
                event_type="budget_exhausted",
                reason=err_msg,
                decision="terminate_tool_calls",
                tool=tool_name,
                arguments=args,
                call_number=self.call_count
            )
            return {
                "success": False,
                "tool": tool_name,
                "call_number": self.call_count,
                "error": err_msg,
                "budget_exhausted": True
            }

        # 3. Validate arguments
        target_fn = self.registry[tool_name]
        validation_error = self._validate_arguments(target_fn, args)
        if validation_error:
            # We don't increment budget on malformed requests that don't hit ERP
            trace_instance.log_event(
                event_type="tool_argument_validation_error",
                reason=f"Argument validation failed for {tool_name}: {validation_error}",
                decision="reject_call",
                tool=tool_name,
                arguments=args
            )
            return {
                "success": False,
                "tool": tool_name,
                "call_number": self.call_count,
                "error": f"Invalid arguments: {validation_error}"
            }

        # 4. Increment call counter before executing valid tool
        self.call_count += 1
        current_call_num = self.call_count

        # 5. Execute tool and catch exceptions
        try:
            raw_result = target_fn(**args)
            
            # Check if internal ERP returned failure
            is_success = True
            if isinstance(raw_result, dict):
                if raw_result.get("found") is False or raw_result.get("success") is False:
                    is_success = False

            # 6. Log call
            trace_instance.log_tool_call(
                tool=tool_name,
                arguments=args,
                call_number=current_call_num,
                success=is_success,
                result=raw_result if is_success else None,
                error=raw_result.get("error") if not is_success and isinstance(raw_result, dict) else None,
                reason=reason
            )

            res = {
                "success": is_success,
                "tool": tool_name,
                "call_number": current_call_num,
                "result": raw_result
            }
            if not is_success and isinstance(raw_result, dict) and "error" in raw_result:
                res["error"] = raw_result["error"]
            return res

        except Exception as e:
            err_str = f"Tool '{tool_name}' failed with exception: {type(e).__name__}: {str(e)}"
            trace_instance.log_tool_call(
                tool=tool_name,
                arguments=args,
                call_number=current_call_num,
                success=False,
                error=err_str,
                reason=reason
            )
            return {
                "success": False,
                "tool": tool_name,
                "call_number": current_call_num,
                "error": err_str
            }


# Global centralized wrapper instance
tool_wrapper_instance = ToolWrapper()


def call_tool(tool_name: str, arguments: Optional[Dict[str, Any]] = None, reason: str = "") -> Dict[str, Any]:
    """Universal gatekeeper function for executing ERP tools."""
    return tool_wrapper_instance.call_tool(tool_name=tool_name, arguments=arguments, reason=reason)
