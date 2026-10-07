"""Main entrypoint for Exam Crisis Desk.

Executes the autonomous agentic workflow to inspect and reschedule
the clashing CSE476 exam within all hard constraints and tool budgets.
"""

from __future__ import annotations
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from agent.agent import ExamCrisisAgent


def main() -> int:
    is_final_run = os.getenv("FINAL_RUN", "false").lower() == "true"
    print("=" * 60)
    print("        EXAM CRISIS DESK — AGENTIC RESCHEDULER        ")
    print(f"        FINAL_RUN MODE: {'ENABLED' if is_final_run else 'DISABLED'}")
    print("=" * 60)

    agent = ExamCrisisAgent(target_exam_id="CSE476")
    result = agent.run()

    print("\n--- AGENT FINAL EXECUTION RESULT ---")
    print(json.dumps(result, indent=2))
    print("=" * 60)

    if result.get("status") == "SUCCESS":
        print(f"SUCCESS: Exam {result.get('exam_id')} successfully verified and rescheduled!")
        print(f"Total Tool Calls: {result.get('tool_calls')} / 40")
        print(f"Total Iterations: {result.get('agent_iterations')} / 15")
        print(f"Audit Trace: {result.get('trace_file')}")
        return 0
    else:
        print(f"TERMINATION: Agent stopped with status '{result.get('status')}': {result.get('error_reason')}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
