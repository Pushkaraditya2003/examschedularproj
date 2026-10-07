"""Master Agent Loop for Exam Crisis Desk.

Executes the university exam rescheduling process:
1. Inspects situation via centralized tool wrapper.
2. Enforces non-bypassable safety gates and task scope restrictions.
3. Generates and prioritizes candidates via Grok Planner.
4. Deterministically verifies all constraints (capacity, room bookings, faculty, students).
5. Requests human approval ONLY for fully verified candidates.
6. Executes irreversible actions in strict verified order.
7. Performs independent post-action and final verification.
8. Logs all operations in Decision Trace.
9. Enforces hard 40-call budget and 15-iteration limit.
"""

from __future__ import annotations
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from agent.planner import planner_instance, GrokPlanner
from agent.safety import safety_gate_instance, SafetyGate, MAX_AGENT_ITERATIONS
from agent.trace import trace_instance, DecisionTrace
from agent.verifier import (
    calculate_working_days,
    required_capacity,
    verify_room_capacity,
    verify_room_availability,
    verify_faculty_availability,
    verify_student_schedules,
    verify_candidate_schedule,
    verify_final_state,
    CandidateVerificationResult,
    FinalVerificationResult
)
from tools.erp import erp_instance
from tools.tool_wrapper import call_tool, tool_wrapper_instance, MAX_TOOL_CALLS


class ExamCrisisAgent:
    """Agent orchestrating the rescheduling of CSE476."""

    def __init__(
        self,
        target_exam_id: str = "CSE476",
        planner: Optional[GrokPlanner] = None,
        safety_gate: Optional[SafetyGate] = None,
        trace: Optional[DecisionTrace] = None
    ) -> None:
        self.target_exam_id = target_exam_id
        self.planner = planner or planner_instance
        self.safety_gate = safety_gate or safety_gate_instance
        self.trace = trace or trace_instance

    def run(self, reset_budget: bool = False) -> Dict[str, Any]:
        """Execute the complete agentic rescheduling workflow."""
        self.trace.reset()
        self.safety_gate.reset()
        if reset_budget:
            tool_wrapper_instance.reset()

        self.trace.log_event(
            event_type="agent_start",
            reason=f"Initiating Exam Crisis Desk workflow for {self.target_exam_id}.",
            decision="start_workflow"
        )

        # Scope validation check
        scope_ok, scope_msg = self.safety_gate.validate_scope(self.target_exam_id, "inspect")
        if not scope_ok:
            return self._terminate_failure(status="SCOPE_ERROR", reason=scope_msg)

        # ---------------- STEP 1: INSPECT EXAM DETAILS ----------------
        can_continue, iter_num, _ = self.safety_gate.increment_iteration()
        exam_res = call_tool(
            "get_exam",
            {"exam_id": self.target_exam_id},
            reason="Retrieve exam details to determine enrollment, date, and assigned rooms."
        )
        if not exam_res["success"] or not exam_res["result"].get("found"):
            return self._terminate_failure("EXAM_NOT_FOUND", "Failed to retrieve exam details.")

        exam_data = exam_res["result"]["exam"]
        original_schedule = {
            "date": exam_data["date"],
            "slot": exam_data["slot"],
            "rooms": exam_data["rooms"],
            "invigilators": exam_data["invigilators"]
        }
        enrollment_count = exam_data["enrollment"]

        # ---------------- STEP 2: ENROLLED STUDENTS ----------------
        students_res = call_tool(
            "get_enrolled_students",
            {"course": self.target_exam_id},
            reason=f"Retrieve full roster of enrolled students for {self.target_exam_id}."
        )
        if not students_res["success"]:
            return self._terminate_failure("ENROLLMENT_RETRIEVAL_FAILED", "Failed to get enrolled students.")
            
        enrolled_students = students_res["result"]["students"]
        if len(enrolled_students) != enrollment_count:
            # Mismatch detection
            self.trace.log_verification_conflict(
                reason="Enrollment count mismatch between get_exam and get_enrolled_students.",
                conflict_details={"exam_enrollment": enrollment_count, "roster_count": len(enrolled_students)}
            )

        # ---------------- STEP 3: READ INBOX (UNTRUSTED DATA) ----------------
        inbox_res = call_tool(
            "read_inbox",
            {},
            reason="Read campus notices to understand clash context and room updates."
        )
        inbox_messages = inbox_res["result"].get("messages", []) if inbox_res["success"] else []
        for msg in inbox_messages:
            # Security scan for prompt injection attempts
            is_safe, injection_reason = self.safety_gate.inspect_untrusted_data("inbox", msg.get("body", ""))
            if not is_safe:
                self.trace.log_event(
                    event_type="security_alert",
                    reason=f"Malicious instruction flagged in message '{msg.get('subject')}': {injection_reason}",
                    decision="ignore_untrusted_instruction_and_continue_safe_scheduling"
                )

        # ---------------- STEP 4: CALCULATE RESCHEDULING WINDOW ----------------
        base_date = exam_data["date"]
        allowed_working_days = calculate_working_days(base_date, count=5)
        self.trace.log_decision(
            reason=f"Dynamically calculated next 5 working days from {base_date}: {allowed_working_days}",
            decision="establish_rescheduling_window",
            allowed_dates=allowed_working_days
        )

        standard_slots = ["10:00", "14:00"]
        rooms_registry = erp_instance.rooms  # Reference for capacity metadata

        # Caches to avoid redundant calls within the single agent run
        cached_faculty: Dict[Tuple[str, str], Dict[str, Any]] = {}
        cached_bookings: Dict[Tuple[str, str], Dict[str, Any]] = {}
        cached_student_schedules: Optional[Dict[str, Any]] = None

        # ---------------- STEP 5: CANDIDATE GENERATION & VERIFICATION LOOP ----------------
        verified_candidate: Optional[Dict[str, Any]] = None
        verified_cand_result: Optional[CandidateVerificationResult] = None

        while verified_candidate is None:
            can_continue, iter_num, iter_msg = self.safety_gate.increment_iteration()
            if not can_continue:
                return self._terminate_failure(
                    status="ITERATION_BUDGET_EXHAUSTED",
                    reason="Unable to produce a verified schedule within the available reasoning/tool budget."
                )

            # Check tool budget status
            if tool_wrapper_instance.call_count >= MAX_TOOL_CALLS - 6:
                return self._terminate_failure(
                    status="TOOL_BUDGET_EXHAUSTED",
                    reason="Stopping safely: approaching 40-tool-call limit without a verified candidate."
                )

            # Generate candidate slots using Grok Planner
            planned_slots = self.planner.plan_candidate_slots(
                exam_info=exam_data,
                allowed_working_days=allowed_working_days,
                standard_slots=standard_slots,
                inbox_summary=inbox_messages,
                rejected_candidates=list(self.safety_gate.rejected_candidates)
            )

            if not planned_slots:
                return self._terminate_failure(
                    status="NO_VIABLE_CANDIDATE",
                    reason="No remaining candidate slots available within working days window."
                )

            # Inspect candidate slots in priority order
            for slot_info in planned_slots:
                cand_date = slot_info["date"]
                cand_slot = slot_info["slot"]
                slot_key = f"{cand_date}:{cand_slot}"

                if self.safety_gate.is_candidate_rejected(slot_key):
                    continue

                self.trace.log_event(
                    event_type="inspecting_candidate_slot",
                    reason=f"Inspecting slot {slot_key}: {slot_info.get('reason')}",
                    decision="gather_slot_evidence",
                    slot_key=slot_key
                )

                # Tool call: list_rooms
                rooms_res = call_tool(
                    "list_rooms",
                    {"date": cand_date, "slot": cand_slot},
                    reason=f"Find rooms reported as available for {cand_date} at {cand_slot}."
                )
                if not rooms_res["success"]:
                    continue

                avail_rooms = rooms_res["result"].get("available_rooms", [])
                if not avail_rooms:
                    self.trace.log_decision(
                        reason=f"No rooms available in slot {slot_key}.",
                        decision="skip_slot"
                    )
                    continue

                # Select best candidate rooms to meet capacity (>= 359 seats)
                sorted_rooms = sorted(avail_rooms, key=lambda r: r.get("capacity", 0), reverse=True)
                selected_rooms: List[str] = []
                current_cap = 0
                req_cap = required_capacity(enrollment_count)

                for r in sorted_rooms:
                    selected_rooms.append(r["room_id"])
                    current_cap += r.get("capacity", 0)
                    if current_cap >= req_cap:
                        break

                if current_cap < req_cap:
                    self.trace.log_decision(
                        reason=f"Available rooms in {slot_key} have combined capacity {current_cap} < required {req_cap}.",
                        decision="skip_slot"
                    )
                    continue

                # Tool call: get_room_bookings for each selected room (independent cross-verification)
                bookings_by_room: Dict[str, Dict[str, Any]] = {}
                contradiction_found = False
                for r_id in selected_rooms:
                    b_cache_key = (r_id, cand_date)
                    if b_cache_key in cached_bookings:
                        bookings_by_room[r_id] = cached_bookings[b_cache_key]
                    else:
                        book_res = call_tool(
                            "get_room_bookings",
                            {"room": r_id, "date": cand_date},
                            reason=f"Cross-verify authoritative booking log for room {r_id} on {cand_date}."
                        )
                        if book_res["success"] and book_res["result"].get("found") is not False:
                            bookings_by_room[r_id] = book_res["result"]
                            cached_bookings[b_cache_key] = book_res["result"]
                        else:
                            contradiction_found = True
                            break

                if contradiction_found:
                    continue

                # Cross-verify room availability immediately
                room_outcome = verify_room_availability(
                    candidate_rooms=selected_rooms,
                    date=cand_date,
                    slot=cand_slot,
                    list_rooms_data=rooms_res["result"],
                    bookings_data_by_room=bookings_by_room
                )
                if not room_outcome.passed:
                    for c in room_outcome.conflicts:
                        self.trace.log_verification_conflict(
                            reason=c.get("issue", "Room contradiction"),
                            conflict_details=c
                        )
                    self.trace.log_decision(
                        reason=f"Room conflict in {slot_key}: {room_outcome.details}",
                        decision="reject_candidate_slot"
                    )
                    continue

                # Tool call: get_faculty_availability for all required invigilators
                faculty_by_id: Dict[str, Dict[str, Any]] = {}
                fac_unavailable = False
                for f_id in exam_data["invigilators"]:
                    f_cache_key = (f_id, cand_date)
                    if f_cache_key in cached_faculty:
                        faculty_by_id[f_id] = cached_faculty[f_cache_key]
                    else:
                        fac_res = call_tool(
                            "get_faculty_availability",
                            {"faculty_id": f_id, "date": cand_date},
                            reason=f"Check availability of invigilator {f_id} on {cand_date}."
                        )
                        if fac_res["success"] and fac_res["result"].get("found") is not False:
                            faculty_by_id[f_id] = fac_res["result"]
                            cached_faculty[f_cache_key] = fac_res["result"]
                        else:
                            fac_unavailable = True
                            break

                if fac_unavailable:
                    continue

                fac_outcome = verify_faculty_availability(
                    required_faculty=exam_data["invigilators"],
                    slot=cand_slot,
                    faculty_data_by_id=faculty_by_id
                )
                if not fac_outcome.passed:
                    self.trace.log_decision(
                        reason=f"Faculty unavailable in {slot_key}: {fac_outcome.details}",
                        decision="reject_candidate_slot"
                    )
                    continue

                # Tool call: get_student_schedule for all enrolled students
                if cached_student_schedules is None:
                    stu_sched_res = call_tool(
                        "get_student_schedule",
                        {"student_ids": enrolled_students},
                        reason="Retrieve exam schedules for enrolled students to verify clashes."
                    )
                    if stu_sched_res["success"]:
                        cached_student_schedules = stu_sched_res["result"]
                    else:
                        continue

                stu_outcome = verify_student_schedules(
                    enrolled_students=enrolled_students,
                    date=cand_date,
                    slot=cand_slot,
                    schedule_data=cached_student_schedules
                )
                if not stu_outcome.passed:
                    self.trace.log_decision(
                        reason=f"Student conflict in {slot_key}: {stu_outcome.details}",
                        decision="reject_candidate_slot"
                    )
                    continue

                # Build candidate object
                candidate_obj = {
                    "exam_id": self.target_exam_id,
                    "date": cand_date,
                    "slot": cand_slot,
                    "rooms": selected_rooms,
                    "invigilators": exam_data["invigilators"],
                    "enrollment": enrollment_count
                }

                # Full deterministic candidate verification
                cand_verification = verify_candidate_schedule(
                    candidate=candidate_obj,
                    exam_details=exam_data,
                    enrolled_students=enrolled_students,
                    allowed_working_days=allowed_working_days,
                    rooms_registry=rooms_registry,
                    list_rooms_data=rooms_res["result"],
                    bookings_data_by_room=bookings_by_room,
                    faculty_data_by_id=faculty_by_id,
                    student_schedule_data=cached_student_schedules
                )

                if cand_verification.is_valid:
                    self.trace.log_candidate_evaluation(
                        candidate=candidate_obj,
                        valid=True,
                        reasons=[],
                        decision="candidate_verified_ready_for_approval"
                    )
                    verified_candidate = candidate_obj
                    verified_cand_result = cand_verification
                    break
                else:
                    self.trace.log_candidate_evaluation(
                        candidate=candidate_obj,
                        valid=False,
                        reasons=cand_verification.reasons,
                        decision="reject_candidate"
                    )

            if verified_candidate is not None:
                break

        # ---------------- STEP 6: HUMAN APPROVAL GATE ----------------
        cand_key = f"{verified_candidate['date']}:{verified_candidate['slot']}:{','.join(verified_candidate['rooms'])}"
        rooms_str = " and ".join(verified_candidate["rooms"])
        approval_summary = (
            f"I verified {verified_candidate['date']} {verified_candidate['slot']}. "
            f"No enrolled student has a conflicting exam, room capacity meets the requirement "
            f"({verified_cand_result.total_capacity} >= {verified_cand_result.required_capacity_val}), "
            f"room bookings are clear, and all required invigilators "
            f"({', '.join(verified_candidate['invigilators'])}) are available. "
            f"Proposed schedule: {verified_candidate['date']} {verified_candidate['slot']} "
            f"using Rooms {rooms_str}. Shall I proceed?"
        )

        approval_res = call_tool(
            "request_human_approval",
            {"summary": approval_summary},
            reason="Request coordinator approval for verified candidate before executing irreversible actions."
        )

        is_approved = approval_res["success"] and approval_res["result"].get("approved", False)
        feedback = approval_res["result"].get("feedback", "") if approval_res["success"] else "No response"
        self.safety_gate.record_human_approval(cand_key, approved=is_approved, feedback=feedback)

        if not is_approved:
            self.trace.log_decision(
                reason=f"Human rejected candidate {cand_key}: {feedback}. Halting irreversible actions.",
                decision="replan_after_rejection"
            )
            return self._terminate_failure(
                status="APPROVAL_REJECTED",
                reason=f"Human approval rejected for candidate {cand_key}: {feedback}"
            )

        # ---------------- STEP 7: IRREVERSIBLE ACTION EXECUTION ----------------
        # 1. Book rooms
        booked_rooms: List[str] = []
        for r_id in verified_candidate["rooms"]:
            gate_ok, gate_err = self.safety_gate.assert_approval_before_irreversible(
                action="book_room",
                candidate_key=cand_key,
                is_candidate_verified=verified_cand_result.is_valid,
                is_approval_granted=is_approved
            )
            if not gate_ok:
                return self._terminate_failure("SAFETY_GATE_VIOLATION", gate_err)

            book_res = call_tool(
                "book_room",
                {"room": r_id, "date": verified_candidate["date"], "slot": verified_candidate["slot"]},
                reason=f"Book verified room {r_id} following human approval."
            )
            if not book_res["success"]:
                return self._terminate_failure(
                    status="ROOM_BOOKING_FAILED",
                    reason=f"Failed to book room {r_id}: {book_res.get('error')}"
                )
            booked_rooms.append(r_id)

        # 2. Reschedule Exam
        gate_ok, gate_err = self.safety_gate.assert_approval_before_irreversible(
            action="reschedule_exam",
            candidate_key=cand_key,
            is_candidate_verified=verified_cand_result.is_valid,
            is_approval_granted=is_approved
        )
        if not gate_ok:
            return self._terminate_failure("SAFETY_GATE_VIOLATION", gate_err)

        resched_res = call_tool(
            "reschedule_exam",
            {
                "exam_id": self.target_exam_id,
                "date": verified_candidate["date"],
                "slot": verified_candidate["slot"],
                "rooms": verified_candidate["rooms"]
            },
            reason=f"Update exam {self.target_exam_id} schedule in university ERP."
        )
        if not resched_res["success"]:
            return self._terminate_failure(
                status="RESCHEDULE_ACTION_FAILED",
                reason=f"Failed to update exam record: {resched_res.get('error')}"
            )

        # 3. Post-Action Independent Verification of Exam State
        post_exam_res = call_tool(
            "get_exam",
            {"exam_id": self.target_exam_id},
            reason="Independently verify updated exam schedule in ERP."
        )

        # 4. Notify Students and Faculty
        gate_ok, gate_err = self.safety_gate.assert_approval_before_irreversible(
            action="send_notice",
            candidate_key=cand_key,
            is_candidate_verified=verified_cand_result.is_valid,
            is_approval_granted=is_approved
        )
        if not gate_ok:
            return self._terminate_failure("SAFETY_GATE_VIOLATION", gate_err)

        notice_message = (
            f"IMPORTANT: {self.target_exam_id} End-term exam has been rescheduled to "
            f"{verified_candidate['date']} at {verified_candidate['slot']} in Rooms "
            f"{', '.join(verified_candidate['rooms'])}."
        )
        notice_res = call_tool(
            "send_notice",
            {"recipients": enrolled_students, "message": notice_message},
            reason=f"Send official exam rescheduling notice to all {len(enrolled_students)} enrolled students."
        )

        # Notification completeness check & retry if partial
        if not notice_res["success"] or notice_res["result"].get("failed_deliveries", 0) > 0:
            failed_recipients = notice_res["result"].get("failed_recipients", [])
            self.trace.log_event(
                event_type="notification_partial_failure",
                reason=f"{len(failed_recipients)} students failed to receive notification. Attempting retry.",
                decision="retry_failed_recipients",
                failed_count=len(failed_recipients)
            )
            # Retry failed recipients if any
            if failed_recipients:
                retry_res = call_tool(
                    "send_notice",
                    {"recipients": failed_recipients, "message": notice_message},
                    reason="Retry notice delivery for students that failed in initial broadcast."
                )
                if retry_res["success"] and retry_res["result"].get("failed_deliveries", 0) == 0:
                    notice_res = {
                        "success": True,
                        "successful_deliveries": len(enrolled_students),
                        "failed_deliveries": 0
                    }

        # ---------------- STEP 8: FINAL INDEPENDENT VERIFICATION ----------------
        final_ver = verify_final_state(
            exam_id=self.target_exam_id,
            candidate=verified_candidate,
            exam_read_result=post_exam_res.get("result", {}),
            approval_granted=is_approved,
            notification_result=notice_res.get("result", notice_res),
            enrolled_count=len(enrolled_students),
            allowed_working_days=allowed_working_days,
            candidate_verification=verified_cand_result
        )

        if not final_ver.success:
            return self._terminate_failure(
                status="FINAL_VERIFICATION_FAILED",
                reason=f"Final state verification failed: {'; '.join(final_ver.details)}"
            )

        # ---------------- STEP 9: SUCCESS TERMINATION & FINAL OUTPUT ----------------
        trace_path = self.trace.save()

        final_output = {
            "status": "SUCCESS",
            "exam_id": self.target_exam_id,
            "original_schedule": original_schedule,
            "new_schedule": {
                "date": verified_candidate["date"],
                "slot": verified_candidate["slot"],
                "rooms": verified_candidate["rooms"],
                "invigilators": verified_candidate["invigilators"],
                "total_room_capacity": verified_cand_result.total_capacity,
                "enrollment": enrollment_count
            },
            "verification": {
                "student_conflicts": False,
                "room_capacity": True,
                "rooms_verified": True,
                "faculty_available": True,
                "approval": True,
                "notifications_complete": True
            },
            "tool_calls": tool_wrapper_instance.call_count,
            "agent_iterations": self.safety_gate.iteration_count,
            "trace_file": trace_path
        }

        self.trace.log_final_result("SUCCESS", final_output)
        self.trace.save()
        return final_output

    def _terminate_failure(self, status: str, reason: str) -> Dict[str, Any]:
        """Terminate the agent safely with audit trace recorded."""
        self.trace.log_event(
            event_type="agent_termination",
            reason=reason,
            decision="halt_workflow",
            status=status
        )
        trace_path = self.trace.save()
        out = {
            "status": status,
            "exam_id": self.target_exam_id,
            "error_reason": reason,
            "tool_calls": tool_wrapper_instance.call_count,
            "agent_iterations": self.safety_gate.iteration_count,
            "trace_file": trace_path
        }
        return out
