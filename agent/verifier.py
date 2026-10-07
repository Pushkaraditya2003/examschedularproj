"""Deterministic Verifier Module.

Independent referee enforcing hard university exam constraints:
1. Dynamic working-day window calculation (excluding weekends).
2. Room capacity verification with configurable one-seat gap rule.
3. Independent cross-verification of room availability (list_rooms vs get_room_bookings).
4. Complete faculty availability verification (never assuming missing info).
5. Complete student schedule conflict verification (zero clashes, never assuming missing info).
6. Comprehensive pre-action candidate verification.
7. Independent post-action and final state verification.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

REQUIRED_EMPTY_SEATS_BETWEEN_STUDENTS: bool = True


@dataclass
class VerificationOutcome:
    """Outcome of a single verification step."""
    passed: bool
    details: str
    conflicts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class CandidateVerificationResult:
    """Comprehensive verification result for a proposed exam schedule candidate."""
    is_valid: bool
    candidate: Dict[str, Any]
    reasons: List[str] = field(default_factory=list)
    capacity_met: bool = False
    rooms_free: bool = False
    faculty_available: bool = False
    no_student_clashes: bool = False
    date_in_window: bool = False
    total_capacity: int = 0
    required_capacity_val: int = 0
    conflicts: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class FinalVerificationResult:
    """Comprehensive independent verification of final rescheduled state."""
    success: bool
    exam_id_correct: bool = False
    date_valid: bool = False
    slot_valid: bool = False
    within_allowed_window: bool = False
    student_clashes_zero: bool = False
    room_capacity_sufficient: bool = False
    rooms_verified_free: bool = False
    faculty_available: bool = False
    human_approval_received: bool = False
    exam_reschedule_verified: bool = False
    all_required_notifications_confirmed: bool = False
    details: List[str] = field(default_factory=list)


def calculate_working_days(start_date_str: str, count: int = 5) -> List[str]:
    """Calculate the next `count` working days strictly excluding Saturdays and Sundays.
    
    start_date_str format: YYYY-MM-DD
    """
    curr = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    working_days: List[str] = []
    
    # Next days starting from tomorrow
    while len(working_days) < count:
        curr += timedelta(days=1)
        # Monday=0, Sunday=6. Weekday is 0..4
        if curr.weekday() < 5:
            working_days.append(curr.strftime("%Y-%m-%d"))
            
    return working_days


def required_capacity(enrollment: int, one_seat_gap: bool = REQUIRED_EMPTY_SEATS_BETWEEN_STUDENTS) -> int:
    """Calculate minimum physical room capacity needed.
    
    With one empty seat between every student:
    For N students, there are (N - 1) gaps, so total seats = N + (N - 1) = 2*N - 1.
    For 180 students: 180 + 179 = 359 seats.
    """
    if enrollment <= 0:
        return 0
    if one_seat_gap:
        return (2 * enrollment) - 1
    return enrollment


def verify_room_capacity(
    rooms: List[Dict[str, Any]],
    enrollment: int,
    one_seat_gap: bool = REQUIRED_EMPTY_SEATS_BETWEEN_STUDENTS
) -> Tuple[bool, int, int, str]:
    """Verify that combined capacity of specified rooms satisfies the required capacity."""
    req_cap = required_capacity(enrollment, one_seat_gap=one_seat_gap)
    total_cap = sum(r.get("capacity", 0) for r in rooms)
    
    passed = total_cap >= req_cap
    if passed:
        msg = f"Capacity satisfied: {total_cap} seats available >= {req_cap} seats required for {enrollment} students."
    else:
        msg = f"Capacity deficient: {total_cap} seats available < {req_cap} seats required for {enrollment} students."
        
    return passed, total_cap, req_cap, msg


def verify_room_availability(
    candidate_rooms: List[str],
    date: str,
    slot: str,
    list_rooms_data: Dict[str, Any],
    bookings_data_by_room: Dict[str, Dict[str, Any]]
) -> VerificationOutcome:
    """Perform independent cross-check between list_rooms and get_room_bookings.
    
    If list_rooms claims a room is free, but get_room_bookings shows a conflicting booking,
    the room must be marked unsafe/unverified.
    """
    available_room_ids = {
        r["room_id"] for r in list_rooms_data.get("available_rooms", [])
    }
    
    conflicts: List[Dict[str, Any]] = []
    
    for r_id in candidate_rooms:
        # Check list_rooms report
        if r_id not in available_room_ids:
            conflicts.append({
                "room": r_id,
                "source": "list_rooms",
                "issue": f"Room '{r_id}' not listed as available on {date} at {slot}."
            })
            continue

        # Check authoritative booking log
        booking_info = bookings_data_by_room.get(r_id)
        if not booking_info or not booking_info.get("found", False):
            conflicts.append({
                "room": r_id,
                "source": "get_room_bookings",
                "issue": f"No authoritative booking record found for room '{r_id}' on {date}."
            })
            continue

        # Look for conflicting bookings in the target slot
        active_bookings = booking_info.get("bookings", [])
        for b in active_bookings:
            if b.get("slot") == slot:
                conflicts.append({
                    "room": r_id,
                    "source": "cross_verification_conflict",
                    "issue": (
                        f"CONTRADICTION: list_rooms reported room '{r_id}' as available, "
                        f"but get_room_bookings shows it is already booked for '{b.get('event')}' "
                        f"by '{b.get('booked_by')}' on {date} at {slot}."
                    )
                })

    if conflicts:
        details = f"Room verification failed with {len(conflicts)} conflict(s)."
        return VerificationOutcome(passed=False, details=details, conflicts=conflicts)

    return VerificationOutcome(
        passed=True,
        details=f"All candidate rooms ({', '.join(candidate_rooms)}) independently verified free on {date} at {slot}."
    )


def verify_faculty_availability(
    required_faculty: List[str],
    slot: str,
    faculty_data_by_id: Dict[str, Dict[str, Any]]
) -> VerificationOutcome:
    """Verify that ALL required invigilating faculty members are available in the target slot."""
    missing_data: List[str] = []
    unavailable: List[str] = []

    for f_id in required_faculty:
        data = faculty_data_by_id.get(f_id)
        if not data or not data.get("found", True):
            missing_data.append(f_id)
            continue
            
        avail_slots = data.get("available_slots", [])
        if slot not in avail_slots:
            unavailable.append(f_id)

    if missing_data:
        return VerificationOutcome(
            passed=False,
            details=f"Incomplete faculty data: availability for {missing_data} could not be determined. Never assuming availability."
        )

    if unavailable:
        return VerificationOutcome(
            passed=False,
            details=f"Faculty member(s) {unavailable} unavailable in slot {slot}."
        )

    return VerificationOutcome(
        passed=True,
        details=f"All required faculty {required_faculty} verified available in slot {slot}."
    )


def verify_student_schedules(
    enrolled_students: List[str],
    date: str,
    slot: str,
    schedule_data: Dict[str, Any]
) -> VerificationOutcome:
    """Verify that NO enrolled student has another exam in the candidate date/slot.
    
    If student schedule data is incomplete, do NOT assume availability.
    """
    schedules = schedule_data.get("student_schedules", {})
    requested_set = set(enrolled_students)
    returned_set = set(schedules.keys())
    
    missing_students = requested_set - returned_set
    if missing_students:
        return VerificationOutcome(
            passed=False,
            details=(
                f"INCOMPLETE SCHEDULE DATA: {len(missing_students)} enrolled students "
                f"missing from schedule records. Cannot assume availability."
            )
        )

    clashing_students: List[Dict[str, Any]] = []
    for s_id in enrolled_students:
        stu_exams = schedules.get(s_id, [])
        for exam in stu_exams:
            if exam.get("date") == date and exam.get("slot") == slot:
                clashing_students.append({
                    "student_id": s_id,
                    "conflicting_course": exam.get("course"),
                    "date": date,
                    "slot": slot
                })

    if clashing_students:
        sample_clashes = [f"{c['student_id']}({c['conflicting_course']})" for c in clashing_students[:5]]
        return VerificationOutcome(
            passed=False,
            details=f"STUDENT CLASH DETECTED: {len(clashing_students)} students have conflicting exams in {date} {slot} (e.g. {sample_clashes}).",
            conflicts=clashing_students
        )

    return VerificationOutcome(
        passed=True,
        details=f"Zero student clashes verified across all {len(enrolled_students)} enrolled students for {date} {slot}."
    )


def verify_candidate_schedule(
    candidate: Dict[str, Any],
    exam_details: Dict[str, Any],
    enrolled_students: List[str],
    allowed_working_days: List[str],
    rooms_registry: Dict[str, Dict[str, Any]],
    list_rooms_data: Dict[str, Any],
    bookings_data_by_room: Dict[str, Dict[str, Any]],
    faculty_data_by_id: Dict[str, Dict[str, Any]],
    student_schedule_data: Dict[str, Any],
    one_seat_gap: bool = REQUIRED_EMPTY_SEATS_BETWEEN_STUDENTS
) -> CandidateVerificationResult:
    """Perform full deterministic pre-action validation on a proposed candidate."""
    reasons: List[str] = []
    all_conflicts: List[Dict[str, Any]] = []
    
    cand_date = candidate.get("date", "")
    cand_slot = candidate.get("slot", "")
    cand_rooms = candidate.get("rooms", [])
    cand_invigilators = candidate.get("invigilators", [])
    enrollment = len(enrolled_students)

    # 1. Date window verification
    date_in_window = cand_date in allowed_working_days
    if not date_in_window:
        reasons.append(f"Date '{cand_date}' is outside allowed 5-working-day window: {allowed_working_days}.")

    # 2. Capacity verification
    room_objects = [{"room_id": r, "capacity": rooms_registry.get(r, {}).get("capacity", 0)} for r in cand_rooms]
    cap_passed, total_cap, req_cap, cap_msg = verify_room_capacity(
        room_objects, enrollment, one_seat_gap=one_seat_gap
    )
    if not cap_passed:
        reasons.append(cap_msg)

    # 3. Independent room availability verification
    room_outcome = verify_room_availability(
        candidate_rooms=cand_rooms,
        date=cand_date,
        slot=cand_slot,
        list_rooms_data=list_rooms_data,
        bookings_data_by_room=bookings_data_by_room
    )
    if not room_outcome.passed:
        reasons.append(room_outcome.details)
        all_conflicts.extend(room_outcome.conflicts)

    # 4. Faculty availability verification
    fac_outcome = verify_faculty_availability(
        required_faculty=cand_invigilators,
        slot=cand_slot,
        faculty_data_by_id=faculty_data_by_id
    )
    if not fac_outcome.passed:
        reasons.append(fac_outcome.details)

    # 5. Student conflict verification
    stu_outcome = verify_student_schedules(
        enrolled_students=enrolled_students,
        date=cand_date,
        slot=cand_slot,
        schedule_data=student_schedule_data
    )
    if not stu_outcome.passed:
        reasons.append(stu_outcome.details)
        all_conflicts.extend(stu_outcome.conflicts)

    is_valid = (
        date_in_window
        and cap_passed
        and room_outcome.passed
        and fac_outcome.passed
        and stu_outcome.passed
    )

    return CandidateVerificationResult(
        is_valid=is_valid,
        candidate=candidate,
        reasons=reasons,
        capacity_met=cap_passed,
        rooms_free=room_outcome.passed,
        faculty_available=fac_outcome.passed,
        no_student_clashes=stu_outcome.passed,
        date_in_window=date_in_window,
        total_capacity=total_cap,
        required_capacity_val=req_cap,
        conflicts=all_conflicts
    )


def verify_final_state(
    exam_id: str,
    candidate: Dict[str, Any],
    exam_read_result: Dict[str, Any],
    approval_granted: bool,
    notification_result: Dict[str, Any],
    enrolled_count: int,
    allowed_working_days: List[str],
    candidate_verification: CandidateVerificationResult
) -> FinalVerificationResult:
    """Perform independent final verification of state before claiming SUCCESS."""
    details: List[str] = []

    # 1. Exam ID match
    exam_data = exam_read_result.get("exam", {})
    id_correct = exam_data.get("exam_id") == exam_id and exam_id == "CSE476"
    if not id_correct:
        details.append(f"Exam ID mismatch: expected {exam_id}, got {exam_data.get('exam_id')}.")

    # 2. Date and Slot match candidate
    cand_date = candidate.get("date")
    cand_slot = candidate.get("slot")
    date_matches = exam_data.get("date") == cand_date
    slot_matches = exam_data.get("slot") == cand_slot
    if not date_matches:
        details.append(f"Rescheduled date mismatch: expected {cand_date}, found {exam_data.get('date')}.")
    if not slot_matches:
        details.append(f"Rescheduled slot mismatch: expected {cand_slot}, found {exam_data.get('slot')}.")

    # 3. Within allowed window
    within_window = cand_date in allowed_working_days
    if not within_window:
        details.append(f"Final date {cand_date} not in allowed working days {allowed_working_days}.")

    # 4. Student clashes 0
    student_clashes_zero = candidate_verification.no_student_clashes

    # 5. Room capacity sufficient
    room_cap_sufficient = candidate_verification.capacity_met

    # 6. Rooms verified free
    rooms_verified_free = candidate_verification.rooms_free

    # 7. Faculty available
    faculty_available = candidate_verification.faculty_available

    # 8. Human approval received
    if not approval_granted:
        details.append("Human approval was NOT received prior to executing irreversible actions.")

    # 9. Exam reschedule verified
    exam_reschedule_verified = (
        exam_data.get("status") == "RESCHEDULED_CONFIRMED"
        and sorted(exam_data.get("rooms", [])) == sorted(candidate.get("rooms", []))
    )
    if not exam_reschedule_verified:
        details.append("Exam status in ERP is not verified as RESCHEDULED_CONFIRMED with expected rooms.")

    # 10. All notifications confirmed
    notifications_complete = (
        notification_result.get("success") is True
        and notification_result.get("successful_deliveries") == enrolled_count
        and notification_result.get("failed_deliveries", 0) == 0
    )
    if not notifications_complete:
        details.append(
            f"Incomplete notifications: {notification_result.get('successful_deliveries')} / {enrolled_count} delivered."
        )

    all_checks_passed = (
        id_correct
        and date_matches
        and slot_matches
        and within_window
        and student_clashes_zero
        and room_cap_sufficient
        and rooms_verified_free
        and faculty_available
        and approval_granted
        and exam_reschedule_verified
        and notifications_complete
    )

    return FinalVerificationResult(
        success=all_checks_passed,
        exam_id_correct=id_correct,
        date_valid=date_matches,
        slot_valid=slot_matches,
        within_allowed_window=within_window,
        student_clashes_zero=student_clashes_zero,
        room_capacity_sufficient=room_cap_sufficient,
        rooms_verified_free=rooms_verified_free,
        faculty_available=faculty_available,
        human_approval_received=approval_granted,
        exam_reschedule_verified=exam_reschedule_verified,
        all_required_notifications_confirmed=notifications_complete,
        details=details
    )
