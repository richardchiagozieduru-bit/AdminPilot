"""
Attendance business logic: building the monthly matrix, saving daily records,
bulk-marking, and computing summaries.

Every mutating function runs inside a transaction and writes an audit entry.
The monthly matrix builder is the heart of the Unified Monthly Sheet — it
pre-fetches all registers and records for a given month, then organises them
into a student × date grid that the template renders directly.
"""

import calendar
from collections import defaultdict
from datetime import date, timedelta

from django.db import transaction
from django.db.models import Count, Q

from academic.models import Session, Term
from core.services import write_audit_log
from students.models import Student, StudentEnrollment, StudentStatus

from .models import AttendanceRecord, AttendanceStatus, ClassAttendanceRegister


# --------------------------------------------------------------------------- #
# Calendar helpers
# --------------------------------------------------------------------------- #
def get_month_school_days(year, month):
    """Return all Monday–Friday dates in the given month, sorted ascending."""
    _, num_days = calendar.monthrange(year, month)
    return [
        date(year, month, day)
        for day in range(1, num_days + 1)
        if date(year, month, day).weekday() < 5  # 0=Mon … 4=Fri
    ]


def get_current_week_days(year, month):
    """Return the Mon-Fri dates of the current week within the given month."""
    today = date.today()
    # Monday of the current week
    monday = today - timedelta(days=today.weekday())
    week_days = []
    for i in range(5):  # Mon-Fri
        d = monday + timedelta(days=i)
        if d.month == month and d.year == year:
            week_days.append(d)
    return week_days


def get_current_school_day(ref_date=None):
    """Return the most appropriate school day (Mon-Fri) for a given date.

    If the date is Saturday or Sunday, returns the preceding Friday.
    """
    if ref_date is None:
        ref_date = date.today()
    if ref_date.weekday() == 5:  # Saturday
        return ref_date - timedelta(days=1)
    elif ref_date.weekday() == 6:  # Sunday
        return ref_date - timedelta(days=2)
    return ref_date


# --------------------------------------------------------------------------- #
# Monthly matrix builder
# --------------------------------------------------------------------------- #
def get_class_monthly_matrix(*, institution_id, klass, year, month, active_date=None):
    """Build the attendance matrix for one class for one month."""
    school_days = get_month_school_days(year, month)
    today = date.today()
    current_school_day = get_current_school_day(today)

    if active_date is None:
        if year == today.year and month == today.month:
            # Current month: default to current school day (today if weekday, Friday if weekend)
            if current_school_day in school_days:
                active_date = current_school_day
            elif school_days:
                active_date = school_days[-1]
        elif year < today.year or (year == today.year and month < today.month):
            # Past month: default to the last school day of that month
            active_date = school_days[-1] if school_days else None
        else:
            # Future month: default to the first school day of that month
            active_date = school_days[0] if school_days else None

    # Get the current session and term for context
    current_session = Session.unscoped.filter(
        institution_id=institution_id, is_current=True
    ).first()
    current_term = Term.unscoped.filter(
        institution_id=institution_id, is_current=True
    ).first()

    # All active enrolled students in this class for the current session/term
    enrollment_filter = {
        "institution_id": institution_id,
        "klass": klass,
        "student__status": StudentStatus.ACTIVE,
    }
    if current_session:
        enrollment_filter["session"] = current_session
    if current_term:
        enrollment_filter["term"] = current_term

    student_ids = (
        StudentEnrollment.unscoped.filter(**enrollment_filter)
        .values_list("student_id", flat=True)
        .distinct()
    )
    students = list(
        Student.unscoped.filter(
            id__in=student_ids, institution_id=institution_id
        ).order_by("last_name", "first_name")
    )

    # All existing registers for this class/month
    registers_qs = ClassAttendanceRegister.unscoped.filter(
        institution_id=institution_id,
        klass=klass,
        date__year=year,
        date__month=month,
    )
    registers_by_date = {r.date: r for r in registers_qs}

    # All records for those registers, keyed by (register_id, student_id)
    register_ids = [r.id for r in registers_qs]
    records_qs = AttendanceRecord.unscoped.filter(
        register_id__in=register_ids
    ).select_related("student")
    records_lookup = {}
    for rec in records_qs:
        records_lookup[(rec.register_id, rec.student_id)] = rec

    # Build student rows
    student_rows = []
    daily_totals = {
        d: {"present": 0, "absent": 0, "late": 0, "excused": 0, "total": 0, "rate": 0}
        for d in school_days
    }

    for student in students:
        cells = []  # One entry per school_day, aligned with school_days
        totals = {"present": 0, "absent": 0, "late": 0, "excused": 0, "rate": 0}
        days_marked = 0

        for d in school_days:
            register = registers_by_date.get(d)
            record = None
            if register:
                record = records_lookup.get((register.id, student.id))

            cells.append({
                "date": d,
                "date_str": d.isoformat(),
                "record": record,
                "status": record.status if record else "",
                "status_display": record.get_status_display() if record else "",
                "remark": record.remark if record else "",
                "is_today": d == date.today(),
                "is_active_day": d == active_date,
            })

            if record:
                days_marked += 1
                if record.status == AttendanceStatus.PRESENT:
                    totals["present"] += 1
                    daily_totals[d]["present"] += 1
                elif record.status == AttendanceStatus.ABSENT:
                    totals["absent"] += 1
                    daily_totals[d]["absent"] += 1
                elif record.status == AttendanceStatus.LATE:
                    totals["late"] += 1
                    daily_totals[d]["late"] += 1
                elif record.status == AttendanceStatus.EXCUSED:
                    totals["excused"] += 1
                    daily_totals[d]["excused"] += 1

        if days_marked > 0:
            totals["rate"] = round(
                (totals["present"] + totals["late"]) / days_marked * 100, 1
            )

        student_rows.append({
            "student": student,
            "cells": cells,
            "totals": totals,
        })

    # Calculate daily totals
    for d in school_days:
        total_marked = (
            daily_totals[d]["present"]
            + daily_totals[d]["absent"]
            + daily_totals[d]["late"]
            + daily_totals[d]["excused"]
        )
        daily_totals[d]["total"] = total_marked
        if total_marked > 0:
            daily_totals[d]["rate"] = round(
                (daily_totals[d]["present"] + daily_totals[d]["late"])
                / total_marked
                * 100,
                1,
            )

    # Convert daily_totals to a list aligned with school_days for template use
    daily_totals_list = [
        {
            "date": d,
            "date_str": d.isoformat(),
            "is_today": d == date.today(),
            "is_active_day": d == active_date,
            **daily_totals[d],
        }
        for d in school_days
    ]

    return {
        "school_days": school_days,
        "students": student_rows,
        "daily_totals": daily_totals,
        "daily_totals_list": daily_totals_list,
        "registers": registers_by_date,
        "active_date": active_date,
        "current_school_day": current_school_day,
        "current_session": current_session,
        "current_term": current_term,
    }


# --------------------------------------------------------------------------- #
# Save daily attendance
# --------------------------------------------------------------------------- #
@transaction.atomic
def save_daily_attendance_for_class(
    *, institution_id, klass, date_obj, records_data, actor, notes="", ip_address=None
):
    """Atomically save/update attendance for one class on one date.

    `records_data` is a list of dicts:
      [{"student_id": int, "status": str, "remark": str}, ...]
    """
    current_session = Session.unscoped.filter(
        institution_id=institution_id, is_current=True
    ).first()
    current_term = Term.unscoped.filter(
        institution_id=institution_id, is_current=True
    ).first()

    register, created = ClassAttendanceRegister.unscoped.get_or_create(
        institution_id=institution_id,
        klass=klass,
        date=date_obj,
        defaults={
            "session": current_session,
            "term": current_term,
            "taken_by": actor,
            "notes": notes,
        },
    )
    if not created:
        register.taken_by = actor
        if notes:
            register.notes = notes
        register.save(update_fields=["taken_by", "notes", "updated_at"])

    for entry in records_data:
        student_id = entry["student_id"]
        status = entry["status"]
        remark = entry.get("remark", "")

        AttendanceRecord.unscoped.update_or_create(
            register=register,
            student_id=student_id,
            defaults={
                "institution_id": institution_id,
                "status": status,
                "remark": remark,
            },
        )

    register.refresh_counts()

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="attendance.recorded",
        summary=(
            f"Attendance recorded for {klass.name} on {date_obj} — "
            f"{register.present_count}P / {register.absent_count}A / "
            f"{register.late_count}L ({register.attendance_rate}%)"
        ),
        target_type="ClassAttendanceRegister",
        target_id=str(register.pk),
        ip_address=ip_address,
    )

    return register


# --------------------------------------------------------------------------- #
# Mark all present
# --------------------------------------------------------------------------- #
@transaction.atomic
def mark_all_present_for_class(
    *, institution_id, klass, date_obj, actor, ip_address=None
):
    """One-click: set all enrolled students to Present for the given date."""
    current_session = Session.unscoped.filter(
        institution_id=institution_id, is_current=True
    ).first()
    current_term = Term.unscoped.filter(
        institution_id=institution_id, is_current=True
    ).first()

    # Get enrolled students
    enrollment_filter = {
        "institution_id": institution_id,
        "klass": klass,
        "student__status": StudentStatus.ACTIVE,
    }
    if current_session:
        enrollment_filter["session"] = current_session
    if current_term:
        enrollment_filter["term"] = current_term

    student_ids = (
        StudentEnrollment.unscoped.filter(**enrollment_filter)
        .values_list("student_id", flat=True)
        .distinct()
    )

    register, _ = ClassAttendanceRegister.unscoped.get_or_create(
        institution_id=institution_id,
        klass=klass,
        date=date_obj,
        defaults={
            "session": current_session,
            "term": current_term,
            "taken_by": actor,
        },
    )

    for sid in student_ids:
        AttendanceRecord.unscoped.update_or_create(
            register=register,
            student_id=sid,
            defaults={
                "institution_id": institution_id,
                "status": AttendanceStatus.PRESENT,
                "remark": "",
            },
        )

    register.refresh_counts()

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="attendance.bulk_present",
        summary=(
            f"All students marked present for {klass.name} on {date_obj} "
            f"({register.total_students} students)"
        ),
        target_type="ClassAttendanceRegister",
        target_id=str(register.pk),
        ip_address=ip_address,
    )

    return register


# --------------------------------------------------------------------------- #
# Clear / Unmark daily attendance
# --------------------------------------------------------------------------- #
@transaction.atomic
def clear_daily_attendance_for_class(
    *, institution_id, klass, date_obj, actor, ip_address=None
):
    """Reset / unmark all student attendance records for one class on one date."""
    register = ClassAttendanceRegister.unscoped.filter(
        institution_id=institution_id,
        klass=klass,
        date=date_obj,
    ).first()

    if register:
        AttendanceRecord.unscoped.filter(register=register).delete()
        register.refresh_counts()

        write_audit_log(
            institution_id=institution_id,
            actor=actor,
            action="attendance.cleared",
            summary=f"Attendance cleared/unmarked for {klass.name} on {date_obj}",
            target_type="ClassAttendanceRegister",
            target_id=str(register.pk),
            ip_address=ip_address,
        )

    return register


# --------------------------------------------------------------------------- #
# Student attendance summary
# --------------------------------------------------------------------------- #
def get_student_attendance_summary(*, student, session=None, term=None):
    """Compute a student's attendance statistics.

    Returns:
      {"present": int, "absent": int, "late": int, "excused": int,
       "total_days": int, "rate": float,
       "recent_absences": [...]}
    """
    filters = {"student": student}
    if session:
        filters["register__session"] = session
    if term:
        filters["register__term"] = term

    records = AttendanceRecord.unscoped.filter(**filters).select_related("register")

    present = records.filter(status=AttendanceStatus.PRESENT).count()
    absent = records.filter(status=AttendanceStatus.ABSENT).count()
    late = records.filter(status=AttendanceStatus.LATE).count()
    excused = records.filter(status=AttendanceStatus.EXCUSED).count()
    total_days = present + absent + late + excused
    rate = round((present + late) / total_days * 100, 1) if total_days > 0 else 0

    # Recent absences/late for the history display
    recent = (
        records.filter(status__in=[AttendanceStatus.ABSENT, AttendanceStatus.LATE])
        .order_by("-register__date")[:10]
    )
    recent_absences = [
        {
            "date": r.register.date,
            "status": r.get_status_display(),
            "remark": r.remark,
        }
        for r in recent
    ]

    return {
        "present": present,
        "absent": absent,
        "late": late,
        "excused": excused,
        "total_days": total_days,
        "rate": rate,
        "recent_absences": recent_absences,
    }
