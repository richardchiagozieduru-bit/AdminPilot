"""
Global search service for AdminPilot.

Searches across Students, Payments, and Receipts within an institution context.
Follows tenant scoping and Bursar field-level restrictions:
  - Owner / Administrator: full student biographical details
  - Bursar: restricted student overview fields (Class, Guardian, Phone, Credit)
"""

from django.db.models import Q
from django.urls import reverse

from billing.models import Payment, Receipt, StudentFeeAssignment
from students.models import Student, StudentStatus


def search_all(*, institution_id, query, is_bursar=False):
    """Perform a full search across Students, Payments, and Receipts.

    Returns a dict:
    {
        "students": [...],
        "payments": [...],
        "receipts": [...],
        "total_count": int,
    }
    """
    query = query.strip()
    if not query:
        return {"students": [], "payments": [], "receipts": [], "total_count": 0}

    # 1. Students
    students_qs = Student.unscoped.filter(
        institution_id=institution_id,
    ).filter(
        Q(first_name__icontains=query)
        | Q(middle_name__icontains=query)
        | Q(last_name__icontains=query)
        | Q(admission_number__icontains=query)
        | Q(guardian_name__icontains=query)
        | Q(guardian_phone__icontains=query)
    ).order_by("first_name", "last_name")[:20]

    student_results = []
    for s in students_qs:
        student_results.append({
            "student": s,
            "url": reverse("students:detail", kwargs={"pk": s.pk}),
            "is_restricted": is_bursar,
        })

    # 2. Payments
    payments_qs = Payment.unscoped.filter(
        institution_id=institution_id,
    ).select_related(
        "assignment__student",
        "assignment__fee_structure",
        "receipt",
    ).filter(
        Q(assignment__student__first_name__icontains=query)
        | Q(assignment__student__last_name__icontains=query)
        | Q(assignment__student__admission_number__icontains=query)
        | Q(receipt__receipt_number__icontains=query)
        | Q(method__icontains=query)
    ).order_by("-payment_date", "-created_at")[:20]

    # 3. Receipts
    receipts_qs = Receipt.unscoped.filter(
        institution_id=institution_id,
    ).select_related(
        "payment__assignment__student",
        "payment__assignment__fee_structure",
    ).filter(
        Q(receipt_number__icontains=query)
        | Q(payment__assignment__student__first_name__icontains=query)
        | Q(payment__assignment__student__last_name__icontains=query)
        | Q(payment__assignment__student__admission_number__icontains=query)
    ).order_by("-created_at")[:20]

    total_count = len(student_results) + len(payments_qs) + len(receipts_qs)

    return {
        "students": student_results,
        "payments": payments_qs,
        "receipts": receipts_qs,
        "total_count": total_count,
    }


def search_suggestions(*, institution_id, query):
    """Generate search-as-you-type suggestions for SearchSuggestView.

    Returns a list of dicts:
    [
        {"title": "Chidi Okeke", "subtitle": "Admission: SCH-2026-000001", "url": "/students/1/", "category": "Student"},
        {"title": "Receipt #SCH-2026-000001", "subtitle": "₦50,000.00 — Chidi Okeke", "url": "/receipts/1/", "category": "Receipt"},
    ]
    """
    query = query.strip()
    if not query or len(query) < 2:
        return []

    results = []

    # 1. Students (max 5)
    students = Student.unscoped.filter(
        institution_id=institution_id,
        status=StudentStatus.ACTIVE,
    ).filter(
        Q(first_name__icontains=query)
        | Q(middle_name__icontains=query)
        | Q(last_name__icontains=query)
        | Q(admission_number__icontains=query)
    )[:5]

    for s in students:
        results.append({
            "title": s.full_name,
            "subtitle": f"Admission {s.admission_number}",
            "url": reverse("students:detail", kwargs={"pk": s.pk}),
            "category": "Student",
        })

    # 2. Receipts (max 3)
    receipts = Receipt.unscoped.filter(
        institution_id=institution_id,
        receipt_number__icontains=query,
    ).select_related("payment__assignment__student")[:3]

    for r in receipts:
        student_name = r.payment.assignment.student.full_name if r.payment else "Unknown"
        results.append({
            "title": f"Receipt {r.receipt_number}",
            "subtitle": f"{r.payment.amount} · {student_name}",
            "url": reverse("billing:receipt_detail", kwargs={"pk": r.pk}),
            "category": "Receipt",
        })

    return results
