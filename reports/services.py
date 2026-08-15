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
