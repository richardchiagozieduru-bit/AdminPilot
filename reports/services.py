"""
Reports calculation services.

All date and period calculations use the institution's timezone (zoneinfo.ZoneInfo),
never server time or UTC, per docs/07_Implementation_Roadmap.md Phase 7.
"""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.db.models import Count, Q, Sum
from django.utils import timezone

from academic.models import Class, ClassStatus, Term
from billing.models import Payment, PaymentStatus, StudentFeeAssignment
from core.models import Institution
from students.models import Student, StudentStatus


def get_institution_timezone(institution_id):
    """Retrieve ZoneInfo for an institution, defaulting to UTC if invalid."""
    institution = Institution.objects.get(pk=institution_id)
    try:
        return ZoneInfo(institution.timezone)
    except (ValueError, Exception):
        return ZoneInfo("UTC")


# --------------------------------------------------------------------------- #
# 1. Income Report
# --------------------------------------------------------------------------- #
def get_income_report_data(
    *,
    institution_id,
    period="term",
    date_from=None,
    date_to=None,
    term_id=None,
):
    """Calculate total income and breakdown by payment method using local timezone boundaries.

    `period` options: "daily", "weekly", "monthly", "term", "custom".
    Returns:
    {
        "period": str,
        "date_from": date,
        "date_to": date,
        "total_collected": Decimal,
        "cash_total": Decimal,
        "transfer_total": Decimal,
        "pos_total": Decimal,
        "payments": QuerySet,
    }
    """
    tz = get_institution_timezone(institution_id)
    today = timezone.now().astimezone(tz).date()

    # Parse date strings if provided
    parsed_date_from = None
    if isinstance(date_from, str) and date_from.strip():
        try:
            parsed_date_from = datetime.date.fromisoformat(date_from.strip())
        except ValueError:
            parsed_date_from = None
    elif isinstance(date_from, datetime.date):
        parsed_date_from = date_from

    parsed_date_to = None
    if isinstance(date_to, str) and date_to.strip():
        try:
            parsed_date_to = datetime.date.fromisoformat(date_to.strip())
        except ValueError:
            parsed_date_to = None
    elif isinstance(date_to, datetime.date):
        parsed_date_to = date_to

    if parsed_date_from or parsed_date_to or period == "custom":
        period = "custom"
        start_date = parsed_date_from or datetime.date(2000, 1, 1)
        end_date = parsed_date_to or datetime.date(2099, 12, 31)
    elif period == "daily":
        start_date = today
        end_date = today
    elif period == "weekly":
        start_date = today - datetime.timedelta(days=today.weekday())
        end_date = start_date + datetime.timedelta(days=6)
    elif period == "monthly":
        start_date = today.replace(day=1)
        next_month = (start_date.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
        end_date = next_month - datetime.timedelta(days=1)
    else:
        # Default: current term or active session
        period = "term"
        current_term = Term.objects.filter(institution_id=institution_id, is_current=True).first()
        if term_id:
            current_term = Term.objects.filter(institution_id=institution_id, pk=term_id).first()
        if current_term:
            start_date = current_term.start_date
            end_date = current_term.end_date
        else:
            start_date = today.replace(day=1)
            end_date = today

    payments_qs = Payment.unscoped.filter(
        institution_id=institution_id,
        status=PaymentStatus.ACTIVE,
        payment_date__gte=start_date,
        payment_date__lte=end_date,
    ).select_related(
        "assignment__student",
        "assignment__fee_structure",
        "receipt",
        "recorded_by",
    ).order_by("-payment_date", "-created_at")

    total_collected = payments_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    cash_total = payments_qs.filter(method="cash").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    transfer_total = payments_qs.filter(method="transfer").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
    pos_total = payments_qs.filter(method="pos").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    return {
        "period": period,
        "date_from": start_date,
        "date_to": end_date,
        "total_collected": total_collected,
        "cash_total": cash_total,
        "transfer_total": transfer_total,
        "pos_total": pos_total,
        "payments": payments_qs,
    }


# --------------------------------------------------------------------------- #
# 2. Outstanding Fees Report
# --------------------------------------------------------------------------- #
def get_outstanding_fees_data(
    *,
    institution_id,
    class_id=None,
    term_id=None,
):
    """Retrieve all student fee assignments with an outstanding balance > 0."""
    assignments = StudentFeeAssignment.unscoped.filter(
        institution_id=institution_id,
    ).select_related(
        "student",
        "fee_structure",
        "fee_structure__klass",
        "fee_structure__term",
    )

    if class_id:
        assignments = assignments.filter(fee_structure__klass_id=class_id)
    if term_id:
        assignments = assignments.filter(fee_structure__term_id=term_id)

    # Outstanding items are those where amount_due > total_paid
    # Outstanding balance is computed property, filter on assignments with remaining balance
    results = [a for a in assignments if a.outstanding_balance > 0]
    total_outstanding = sum(a.outstanding_balance for a in results)
    total_due = sum(a.amount_due for a in results)

    return {
        "assignments": results,
        "total_outstanding": total_outstanding,
        "total_due": total_due,
        "selected_class_id": class_id,
        "selected_term_id": term_id,
    }


# --------------------------------------------------------------------------- #
# 3. Student Payment History Report
# --------------------------------------------------------------------------- #
def get_student_payment_history_data(*, institution_id, student_id):
    """Retrieve comprehensive billing statement for a single student."""
    student = Student.unscoped.get(institution_id=institution_id, pk=student_id)

    assignments = StudentFeeAssignment.unscoped.filter(
        institution_id=institution_id,
        student=student,
    ).select_related(
        "fee_structure",
        "fee_structure__klass",
        "fee_structure__term",
    ).order_by("-created_at")

    payments = Payment.unscoped.filter(
        institution_id=institution_id,
        assignment__student=student,
    ).select_related(
        "assignment__fee_structure",
        "receipt",
        "recorded_by",
    ).order_by("-payment_date", "-created_at")

    total_billed = sum(a.amount_due for a in assignments)
    total_paid = sum(p.amount for p in payments if p.status == PaymentStatus.ACTIVE)
    total_outstanding = sum(a.outstanding_balance for a in assignments)

    return {
        "student": student,
        "assignments": assignments,
        "payments": payments,
        "total_billed": total_billed,
        "total_paid": total_paid,
        "total_outstanding": total_outstanding,
        "credit_balance": student.credit_balance,
    }


# --------------------------------------------------------------------------- #
# 4. Class Summary Report
# --------------------------------------------------------------------------- #
def get_class_summary_data(
    *,
    institution_id,
    class_id=None,
    term_id=None,
):
    """Compute high-level fee summary per active class.

    Returns a list of class summary dicts:
    [
        {
            "class": Class,
            "student_count": int,
            "total_billed": Decimal,
            "total_collected": Decimal,
            "total_outstanding": Decimal,
            "fully_paid_count": int,
        },
        ...
    ]
    """
    classes_qs = Class.unscoped.filter(
        institution_id=institution_id,
        status=ClassStatus.ACTIVE,
    ).order_by("order", "name")

    if class_id:
        classes_qs = classes_qs.filter(pk=class_id)

    summaries = []
    grand_billed = Decimal("0.00")
    grand_collected = Decimal("0.00")
    grand_outstanding = Decimal("0.00")

    for klass in classes_qs:
        assignments = StudentFeeAssignment.unscoped.filter(
            institution_id=institution_id,
            fee_structure__klass=klass,
        )
        if term_id:
            assignments = assignments.filter(fee_structure__term_id=term_id)

        student_count = assignments.values("student").distinct().count()
        total_billed = assignments.aggregate(total=Sum("amount_due"))["total"] or Decimal("0.00")
        
        # Outstanding is sum of outstanding balances
        assignment_list = list(assignments)
        total_outstanding = sum(a.outstanding_balance for a in assignment_list)
        total_collected = total_billed - total_outstanding
        if total_collected < 0:
            total_collected = Decimal("0.00")

        fully_paid_count = sum(1 for a in assignment_list if a.outstanding_balance <= 0)

        grand_billed += total_billed
        grand_collected += total_collected
        grand_outstanding += total_outstanding

        summaries.append({
            "class": klass,
            "student_count": student_count,
            "total_billed": total_billed,
            "total_collected": total_collected,
            "total_outstanding": total_outstanding,
            "fully_paid_count": fully_paid_count,
        })

    return {
        "summaries": summaries,
        "grand_billed": grand_billed,
        "grand_collected": grand_collected,
        "grand_outstanding": grand_outstanding,
    }


# --------------------------------------------------------------------------- #
# 5. Fee Intelligence & Payment Analytics
# --------------------------------------------------------------------------- #
def get_fee_intelligence_data(
    *,
    institution_id,
    fee_item_name=None,
    class_id=None,
    session_id=None,
    term_id=None,
    status_filter="all",
    date_from=None,
    date_to=None,
    method=None,
):
    """Calculates item-level billing, collection, and outstanding metrics for a specific fee component.
    
    Powers the Fee Intelligence & Item Analytics report.
    """
    from billing.models import (
        FeeStructureItem,
        PaymentItemAllocation,
        PaymentStatus,
        StudentFeeAssignment,
        StudentFeeItem,
    )

    # 1. Discover all distinct fee item names across the school
    fs_items = (
        FeeStructureItem.unscoped.filter(fee_structure__institution_id=institution_id)
        .values_list("name", flat=True)
        .distinct()
    )
    s_items = (
        StudentFeeItem.unscoped.filter(institution_id=institution_id)
        .values_list("name", flat=True)
        .distinct()
    )
    distinct_item_names = sorted(list(set(list(fs_items) + list(s_items))))

    selected_item_name = fee_item_name
    if not selected_item_name and distinct_item_names:
        # Default to first fee item (e.g. Tuition)
        selected_item_name = distinct_item_names[0]

    assignments_qs = (
        StudentFeeAssignment.unscoped.filter(institution_id=institution_id)
        .select_related(
            "student",
            "fee_structure",
            "fee_structure__klass",
            "fee_structure__term",
            "fee_structure__session",
        )
    )
    if class_id:
        assignments_qs = assignments_qs.filter(fee_structure__klass_id=class_id)
    if term_id:
        assignments_qs = assignments_qs.filter(fee_structure__term_id=term_id)
    if session_id:
        assignments_qs = assignments_qs.filter(fee_structure__session_id=session_id)

    student_rows = []
    class_summary_map = {}

    overall_billed = Decimal("0.00")
    overall_collected = Decimal("0.00")
    overall_outstanding = Decimal("0.00")
    overall_paid_count = 0
    overall_partial_count = 0
    overall_unpaid_count = 0
    overall_total_students = 0

    for assignment in assignments_qs:
        breakdown = assignment.get_item_breakdown()
        target_item = None
        for item_data in breakdown:
            if item_data.get("name", "").strip().lower() == (selected_item_name or "").strip().lower():
                target_item = item_data
                break

        if not target_item:
            continue

        item_billed = target_item.get("billed") or target_item.get("amount") or Decimal("0.00")
        item_paid = target_item.get("paid") or Decimal("0.00")
        item_remaining = target_item.get("remaining") or Decimal("0.00")
        item_status = target_item.get("status", "unpaid")  # 'paid', 'partial', 'unpaid'

        klass = assignment.fee_structure.klass
        k_id = klass.pk

        # Track class summaries
        if k_id not in class_summary_map:
            class_summary_map[k_id] = {
                "class": klass,
                "student_count": 0,
                "billed": Decimal("0.00"),
                "collected": Decimal("0.00"),
                "outstanding": Decimal("0.00"),
                "paid_count": 0,
            }
        class_summary_map[k_id]["student_count"] += 1
        class_summary_map[k_id]["billed"] += item_billed
        class_summary_map[k_id]["collected"] += item_paid
        class_summary_map[k_id]["outstanding"] += item_remaining
        if item_status == "paid":
            class_summary_map[k_id]["paid_count"] += 1

        overall_total_students += 1
        overall_billed += item_billed
        overall_collected += item_paid
        overall_outstanding += item_remaining
        if item_status == "paid":
            overall_paid_count += 1
        elif item_status == "partial":
            overall_partial_count += 1
        else:
            overall_unpaid_count += 1

        # Check status filter
        if status_filter == "paid" and item_status != "paid":
            continue
        elif status_filter == "partial" and item_status != "partial":
            continue
        elif status_filter == "unpaid" and item_status != "unpaid":
            continue

        # Find latest payment for this assignment
        active_payments = assignment.payments.filter(status=PaymentStatus.ACTIVE)
        if method:
            active_payments = active_payments.filter(method=method)
        if date_from:
            active_payments = active_payments.filter(payment_date__gte=date_from)
        if date_to:
            active_payments = active_payments.filter(payment_date__lte=date_to)

        latest_payment = active_payments.order_by("-payment_date", "-created_at").first()

        student_rows.append({
            "assignment": assignment,
            "student": assignment.student,
            "klass": klass,
            "fee_structure": assignment.fee_structure,
            "item_name": selected_item_name,
            "item_billed": item_billed,
            "item_paid": item_paid,
            "item_remaining": item_remaining,
            "item_status": item_status,
            "last_payment_date": latest_payment.payment_date if latest_payment else None,
            "last_payment_method": latest_payment.get_method_display() if latest_payment else None,
        })

    collection_rate = (
        round(overall_collected / overall_billed * 100, 1)
        if overall_billed > 0
        else 0.0
    )

    class_summaries = []
    for c_info in sorted(
        class_summary_map.values(), key=lambda x: (x["class"].order, x["class"].name)
    ):
        c_rate = (
            round(c_info["collected"] / c_info["billed"] * 100, 1)
            if c_info["billed"] > 0
            else 0.0
        )
        c_info["collection_rate"] = c_rate
        class_summaries.append(c_info)

    student_rows.sort(
        key=lambda r: (
            r["klass"].order,
            r["klass"].name,
            r["student"].last_name,
            r["student"].first_name,
        )
    )

    return {
        "distinct_item_names": distinct_item_names,
        "selected_item_name": selected_item_name,
        "student_rows": student_rows,
        "class_summaries": class_summaries,
        "total_billed": overall_billed,
        "total_collected": overall_collected,
        "total_outstanding": overall_outstanding,
        "collection_rate": collection_rate,
        "total_students": overall_total_students,
        "paid_students": overall_paid_count,
        "partial_students": overall_partial_count,
        "unpaid_students": overall_unpaid_count,
    }
