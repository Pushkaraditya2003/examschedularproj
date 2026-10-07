"""Unit tests for tool wrapper, budget tracking, and core constraints."""

import pytest
from tools.erp import erp_instance
from tools.tool_wrapper import tool_wrapper_instance, call_tool, MAX_TOOL_CALLS
from agent.trace import trace_instance


@pytest.fixture(autouse=True)
def reset_all():
    erp_instance.reset()
    tool_wrapper_instance.reset()
    trace_instance.reset()
    yield
    erp_instance.reset()
    tool_wrapper_instance.reset()
    trace_instance.reset()


def test_tool_wrapper_success():
    res = call_tool("get_exam", {"exam_id": "CSE476"}, reason="Get exam specs")
    assert res["success"] is True
    assert res["tool"] == "get_exam"
    assert res["call_number"] == 1
    assert res["result"]["found"] is True
    assert tool_wrapper_instance.call_count == 1


def test_tool_wrapper_unknown_tool():
    res = call_tool("delete_database", {}, reason="Malicious call attempt")
    assert res["success"] is False
    assert "Unknown or disallowed tool" in res["error"]
    assert tool_wrapper_instance.call_count == 0  # Does not count towards ERP budget


def test_tool_wrapper_argument_validation():
    # Missing required argument 'exam_id'
    res = call_tool("get_exam", {}, reason="Missing argument")
    assert res["success"] is False
    assert "Missing required parameter" in res["error"]
    assert tool_wrapper_instance.call_count == 0


def test_tool_wrapper_catches_exception_safely():
    erp_instance.failure_config.tool_exception = "read_inbox"
    res = call_tool("read_inbox", {}, reason="Test exception handling")
    assert res["success"] is False
    assert res["call_number"] == 1
    assert "failed with exception" in res["error"]
    # Agent did not crash


def test_tool_wrapper_budget_hard_cap():
    # Exhaust all 40 calls
    for i in range(MAX_TOOL_CALLS):
        res = call_tool("read_inbox", {}, reason=f"Budget test call {i+1}")
        assert res["call_number"] == i + 1

    assert tool_wrapper_instance.call_count == 40

    # 41st call must be blocked
    res_blocked = call_tool("read_inbox", {}, reason="Excess call")
    assert res_blocked["success"] is False
    assert res_blocked.get("budget_exhausted") is True
    assert "HARD BUDGET EXHAUSTED" in res_blocked["error"]
    # Call count must stay exactly 40
    assert tool_wrapper_instance.call_count == 40


def test_decision_trace_logging():
    call_tool("get_exam", {"exam_id": "CSE476"}, reason="Trace test")
    trace_events = trace_instance.events
    assert len(trace_events) >= 1
    last_event = trace_events[-1]
    assert last_event["type"] == "tool_call"
    assert last_event["tool"] == "get_exam"
    assert last_event["arguments"] == {"exam_id": "CSE476"}


# ---------------- VERIFIER TESTS ----------------

from agent.verifier import (
    calculate_working_days,
    required_capacity,
    verify_room_capacity,
    verify_room_availability,
    verify_faculty_availability,
    verify_student_schedules,
    verify_candidate_schedule,
    verify_final_state,
    CandidateVerificationResult
)


def test_working_days_calculation():
    # Friday 2026-10-09 -> Next 5 working days must skip Sat 10-10 and Sun 10-11
    days = calculate_working_days("2026-10-09", count=5)
    assert days == [
        "2026-10-12",  # Mon
        "2026-10-13",  # Tue
        "2026-10-14",  # Wed
        "2026-10-15",  # Thu
        "2026-10-16",  # Fri
    ]


def test_required_capacity_rule():
    # Conservative one-seat gap: 180 students -> 180 + 179 = 359
    assert required_capacity(180, one_seat_gap=True) == 359
    assert required_capacity(180, one_seat_gap=False) == 180
    assert required_capacity(0) == 0


def test_multi_room_capacity_verification():
    rooms_registry = {
        "HALL-A": {"capacity": 200},
        "HALL-B": {"capacity": 180},
        "LH-101": {"capacity": 100}
    }
    # Combined HALL-A (200) + HALL-B (180) = 380 >= 359
    passed, total_cap, req_cap, _ = verify_room_capacity(
        [rooms_registry["HALL-A"], rooms_registry["HALL-B"]],
        enrollment=180
    )
    assert passed is True
    assert total_cap == 380
    assert req_cap == 359

    # LH-101 alone = 100 < 359
    passed_fail, total_cap_fail, _, _ = verify_room_capacity(
        [rooms_registry["LH-101"]],
        enrollment=180
    )
    assert passed_fail is False
    assert total_cap_fail == 100


def test_room_cross_verification_detects_conflict():
    list_rooms_data = {
        "available_rooms": [{"room_id": "AUD-1"}, {"room_id": "HALL-A"}]
    }
    # get_room_bookings shows AUD-1 has a booking at 10:00
    bookings_data = {
        "AUD-1": {
            "found": True,
            "bookings": [{"slot": "10:00", "event": "Maintenance", "booked_by": "Estates"}]
        },
        "HALL-A": {
            "found": True,
            "bookings": []
        }
    }

    outcome = verify_room_availability(
        candidate_rooms=["AUD-1"],
        date="2026-10-13",
        slot="10:00",
        list_rooms_data=list_rooms_data,
        bookings_data_by_room=bookings_data
    )
    assert outcome.passed is False
    assert len(outcome.conflicts) == 1
    assert "CONTRADICTION" in outcome.conflicts[0]["issue"]

    # HALL-A has no booking and is listed as available
    outcome_ok = verify_room_availability(
        candidate_rooms=["HALL-A"],
        date="2026-10-13",
        slot="10:00",
        list_rooms_data=list_rooms_data,
        bookings_data_by_room=bookings_data
    )
    assert outcome_ok.passed is True


def test_faculty_availability_verification():
    faculty_data = {
        "FAC001": {"found": True, "available_slots": ["09:00", "10:00"]},
        "FAC002": {"found": True, "available_slots": ["10:00", "14:00"]},
        "FAC003": {"found": True, "available_slots": ["14:00"]}  # Not available at 10:00
    }

    # FAC001 and FAC002 available at 10:00
    res_ok = verify_faculty_availability(["FAC001", "FAC002"], "10:00", faculty_data)
    assert res_ok.passed is True

    # FAC003 unavailable at 10:00
    res_fail = verify_faculty_availability(["FAC001", "FAC003"], "10:00", faculty_data)
    assert res_fail.passed is False
    assert "unavailable" in res_fail.details

    # Missing faculty data
    res_missing = verify_faculty_availability(["FAC999"], "10:00", faculty_data)
    assert res_missing.passed is False
    assert "Incomplete faculty data" in res_missing.details


def test_student_schedule_verification():
    students = ["S1", "S2", "S3"]
    schedule_data = {
        "student_schedules": {
            "S1": [{"date": "2026-10-12", "slot": "10:00", "course": "MTH101"}],
            "S2": [],
            "S3": []
        }
    }

    # Clash on 2026-10-12 at 10:00 for S1
    clash_res = verify_student_schedules(students, "2026-10-12", "10:00", schedule_data)
    assert clash_res.passed is False
    assert "STUDENT CLASH DETECTED" in clash_res.details

    # No clash on 2026-10-13 at 10:00
    no_clash_res = verify_student_schedules(students, "2026-10-13", "10:00", schedule_data)
    assert no_clash_res.passed is True

    # Incomplete student data must not assume availability
    incomplete_sched = {"student_schedules": {"S1": []}}  # S2, S3 missing
    incomp_res = verify_student_schedules(students, "2026-10-13", "10:00", incomplete_sched)
    assert incomp_res.passed is False
    assert "INCOMPLETE SCHEDULE DATA" in incomp_res.details

