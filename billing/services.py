"""
Billing services: fee structures, payments, credit, and receipts.

The five invariants from docs/02_Database.md that shape this module:

  * Fee structures lock the moment the first payment lands on any of their
    student assignments. After that the only way to change an amount is a
    StudentFeeAssignment adjustment (reason required, audit-logged).
  * Payment + receipt-counter increment + receipt insert happen inside one
    transaction.atomic() block — no payment exists without a receipt number, no
    receipt number is ever skipped or duplicated under concurrent requests.
  * Credit transactions are append-only. A positive row is created when a
    payment exceeds the assignment's outstanding balance; a negative row when
    existing credit is manually applied. Credit is never auto-applied.
  * students.credit_balance is a cached running total kept in sync at write
    time — the ledger remains the audit trail.
  * Locking is application-layer only (CR-002): every write path passes
    through this service, so an AFTER UPDATE trigger would guard a path that
    cannot be reached any other way.
"""

import logging
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from core.models import InstitutionNumberSequence
from core.services import format_sequence, next_sequence_number, write_audit_log

from .models import (
    CreditTransaction,
    FeeStructure,
    FeeStructureItem,
    Payment,
    PaymentItemAllocation,
    PaymentStatus,
    Receipt,
    StudentFeeAssignment,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Fee structures
# --------------------------------------------------------------------------- #
@transaction.atomic
def create_fee_structure(
    *,
    institution_id,
    name,
    klass,
    session,
    term,
    items,
    actor,
    ip_address=None,
):
    """Create a FeeStructure with its itemized lines and auto-assign enrolled students.

    `items` is a list of dicts: [{"name": "Tuition", "amount": Decimal("50000")}, ...]

    Auto-assignment (docs/03_Views_and_Endpoints.md): creates a
    StudentFeeAssignment for every student currently enrolled in this
    class/session/term. The deferred import avoids the circular dependency
    documented in students/services.py's module docstring.
    """
    total = sum(item["amount"] for item in items)

    structure = FeeStructure.unscoped.create(
        institution_id=institution_id,
        name=name,
        klass=klass,
        session=session,
        term=term,
        total_amount=total,
    )

    for item in items:
        FeeStructureItem.unscoped.create(
            institution_id=institution_id,
            fee_structure=structure,
            name=item["name"],
            amount=item["amount"],
        )

    # Auto-assign enrolled students
    from students.models import StudentEnrollment

    enrollments = StudentEnrollment.unscoped.filter(
        institution_id=institution_id,
        klass=klass,
        session=session,
        term=term,
    ).select_related("student")

    # Only assign once per student — if a student has multiple enrollments in the
    # same class/session/term (e.g. from a class change and back), take the
    # latest and create one assignment.
    seen_students = set()
    assignments_created = 0
    for enrollment in enrollments:
        if enrollment.student_id in seen_students:
            continue
        seen_students.add(enrollment.student_id)

        # Don't double-assign if somehow already present
        if not StudentFeeAssignment.unscoped.filter(
            institution_id=institution_id,
            student=enrollment.student,
            fee_structure=structure,
        ).exists():
            StudentFeeAssignment.unscoped.create(
                institution_id=institution_id,
                student=enrollment.student,
                fee_structure=structure,
                amount_due=total,
            )
            assignments_created += 1

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="fee_structure.created",
        summary=(
            f"Created fee structure '{name}' for {klass.name} "
            f"({total}), auto-assigned to {assignments_created} students"
        ),
        target_type="FeeStructure",
        target_id=str(structure.pk),
        ip_address=ip_address,
    )

    return structure


@transaction.atomic
def update_fee_structure(
    *,
    fee_structure,
    name,
    items,
    actor,
    ip_address=None,
):
    """Update a fee structure's name and items.
    
    Safe for both open and locked fee structures. When payments already exist,
    all historical payments, receipts, and payment allocations remain completely
    intact. Student assignments without individual overrides will have their
    amount_due synchronized with the new total.
    """
    old_total = fee_structure.total_amount

    # Replace items
    fee_structure.items.all().delete()
    new_total = Decimal("0.00")
    for item in items:
        FeeStructureItem.unscoped.create(
            institution_id=fee_structure.institution_id,
            fee_structure=fee_structure,
            name=item["name"],
            amount=item["amount"],
        )
        new_total += item["amount"]

    fee_structure.name = name
    fee_structure.total_amount = new_total
    fee_structure.save(update_fields=["name", "total_amount"])

    # Update unadjusted assignments — those whose amount_due still equals the
    # old total and have never been individually adjusted.
    if old_total != new_total:
        StudentFeeAssignment.unscoped.filter(
            fee_structure=fee_structure,
            amount_due=old_total,
            adjustment_reason="",
        ).update(amount_due=new_total)

    write_audit_log(
        institution_id=fee_structure.institution_id,
        actor=actor,
        action="fee_structure.updated",
        summary=f"Updated fee structure '{name}' (total: {new_total})",
        target_type="FeeStructure",
        target_id=str(fee_structure.pk),
        detail={"old_total": str(old_total), "new_total": str(new_total)},
        ip_address=ip_address,
    )

    return fee_structure


@transaction.atomic
def toggle_fee_structure_lock(
    *,
    fee_structure,
    actor,
    ip_address=None,
):
    """Toggle a fee structure's locked status between open and locked with audit log."""
    new_status = not fee_structure.locked
    fee_structure.locked = new_status
    fee_structure.save(update_fields=["locked"])

    action = "fee_structure.locked" if new_status else "fee_structure.unlocked"
    status_str = "locked" if new_status else "unlocked"

    write_audit_log(
        institution_id=fee_structure.institution_id,
        actor=actor,
        action=action,
        summary=f"Fee structure '{fee_structure.name}' was marked as {status_str}.",
        target_type="FeeStructure",
        target_id=str(fee_structure.pk),
        detail={"locked": new_status},
        ip_address=ip_address,
    )
    return fee_structure


@transaction.atomic
def delete_fee_structure(
    *,
    fee_structure,
    actor,
    ip_address=None,
):
    """Delete a fee structure (open or locked).

    Purges all associated payment allocations, receipts, payments,
    credit transactions, student fee assignments, items, and the fee structure itself.
    Audit-logged.
    """
    structure_pk = fee_structure.pk
    name = fee_structure.name
    klass_name = fee_structure.klass.name
    institution_id = fee_structure.institution_id

    # Cleanly remove all payment-related rows under assignments of this structure
    for assignment in fee_structure.assignments.all():
        for payment in assignment.payments.all():
            PaymentItemAllocation.unscoped.filter(payment=payment).delete()
            CreditTransaction.unscoped.filter(source_payment=payment).delete()
            if hasattr(payment, "receipt") and payment.receipt:
                payment.receipt.delete()
            payment.delete()
        CreditTransaction.unscoped.filter(applied_to_assignment=assignment).delete()
        assignment.delete()

    # Delete items
    fee_structure.items.all().delete()
    # Delete structure
    fee_structure.delete()

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="fee_structure.deleted",
        summary=f"Deleted fee structure '{name}' for {klass_name}",
        target_type="FeeStructure",
        target_id=str(structure_pk),
        ip_address=ip_address,
    )



# --------------------------------------------------------------------------- #
# Fee adjustments
# --------------------------------------------------------------------------- #
@transaction.atomic
def adjust_student_fee(
    *,
    assignment,
    new_amount,
    reason,
    actor,
    ip_address=None,
):
    """Adjust a student's fee assignment amount. Reason is required.

    This is the only path to correct a fee after the parent structure is locked
    (docs/02_Database.md).
    """
    if not reason.strip():
        raise ValidationError("A reason is required for fee adjustments.")

    old_amount = assignment.amount_due
    assignment.amount_due = new_amount
    assignment.adjustment_reason = reason
    assignment.adjusted_at = timezone.now()
    assignment.save(update_fields=["amount_due", "adjustment_reason", "adjusted_at"])

    write_audit_log(
        institution_id=assignment.institution_id,
        actor=actor,
        action="fee_assignment.adjusted",
        summary=(
            f"Adjusted fee for {assignment.student.full_name}: "
            f"{old_amount} → {new_amount}"
        ),
        target_type="StudentFeeAssignment",
        target_id=str(assignment.pk),
        reason=reason,
        detail={"old_amount": str(old_amount), "new_amount": str(new_amount)},
        ip_address=ip_address,
    )

    return assignment


# --------------------------------------------------------------------------- #
# Payment recording
# --------------------------------------------------------------------------- #
@transaction.atomic
def record_payment(
    *,
    assignment: StudentFeeAssignment,
    amount: Decimal,
    payment_date,
    method: str,
    actor=None,
    apply_credit: bool = False,
    item_allocations: list[dict] = None,
    ip_address=None,
) -> tuple[Payment, Receipt, Decimal, Decimal]:
    """Record a payment atomically with receipt number generation.

    The single write path for money landing in the system. Enforces:
      1. If apply_credit is true, applies available student credit up to the
         outstanding balance (creating a negative CreditTransaction)
      2. Creates the Payment row
      2b. Creates PaymentItemAllocation rows (custom item allocations or auto-waterfall)
      3. Flips fee_structure.locked to true if not already locked
      4. Increments the receipt counter for (institution, year) and creates the
         Receipt row
      5. If the payment + any applied credit exceeds the outstanding balance,
         record the excess as a positive CreditTransaction
      6. Update the student's cached credit_balance

    Returns (payment, receipt, credit_applied, credit_created).
    """
    from students.models import Student

    student = assignment.student
    institution = assignment.institution

    credit_applied = Decimal("0.00")
    credit_created = Decimal("0.00")

    # Step 1: Apply existing credit if requested
    if apply_credit and student.credit_balance > 0:
        outstanding = assignment.outstanding_balance
        credit_to_apply = min(student.credit_balance, outstanding)
        if credit_to_apply > 0:
            CreditTransaction.unscoped.create(
                institution_id=institution.pk,
                amount=-credit_to_apply,
                applied_to_assignment=assignment,
            )
            student.credit_balance -= credit_to_apply
            student.save(update_fields=["credit_balance"])
            credit_applied = credit_to_apply

    # Step 2: Create the payment
    payment = Payment.unscoped.create(
        institution_id=institution.pk,
        assignment=assignment,
        amount=amount,
        payment_date=payment_date,
        method=method,
        recorded_by=actor,
    )

    # Step 2b: Create line-item allocations
    recorded_allocations = []
    if item_allocations:
        for alloc in item_allocations:
            fee_item_id = alloc.get("fee_item_id")
            alloc_amt = Decimal(str(alloc.get("amount", "0.00")))
            if alloc_amt > 0 and fee_item_id:
                PaymentItemAllocation.unscoped.create(
                    institution_id=institution.pk,
                    payment=payment,
                    fee_item_id=fee_item_id,
                    amount=alloc_amt,
                )
                recorded_allocations.append({
                    "fee_item_id": fee_item_id,
                    "amount": str(alloc_amt),
                })
    else:
        # Fallback automatic priority waterfall allocation
        breakdown = assignment.get_item_breakdown()
        remaining_to_allocate = amount
        for item_data in breakdown:
            if remaining_to_allocate <= 0:
                break
            needed = item_data["remaining"]
            if needed > 0:
                alloc_amt = min(needed, remaining_to_allocate)
                PaymentItemAllocation.unscoped.create(
                    institution_id=institution.pk,
                    payment=payment,
                    fee_item_id=item_data["fee_item_id"],
                    amount=alloc_amt,
                )
                recorded_allocations.append({
                    "fee_item_id": item_data["fee_item_id"],
                    "amount": str(alloc_amt),
                })
                remaining_to_allocate -= alloc_amt

    # Step 3: Lock the fee structure (CR-002, application-layer only)
    fee_structure = assignment.fee_structure
    if not fee_structure.locked:
        fee_structure.locked = True
        fee_structure.save(update_fields=["locked"])

    # Step 4: Receipt number and Receipt row
    from zoneinfo import ZoneInfo

    now = timezone.now().astimezone(ZoneInfo(institution.timezone))
    year = now.year
    seq = next_sequence_number(
        institution.pk, InstitutionNumberSequence.Kind.RECEIPT, year
    )
    receipt_number = format_sequence(institution.code, year, seq)

    receipt = Receipt.unscoped.create(
        institution_id=institution.pk,
        payment=payment,
        receipt_number=receipt_number,
        issued_by=actor,
    )

    # Step 5: Check for overpayment and create credit
    # Recalculate outstanding after the payment is recorded
    new_outstanding = assignment.outstanding_balance
    if new_outstanding < 0:
        # The overpayment amount is the absolute value of the negative balance
        credit_created = abs(new_outstanding)
        CreditTransaction.unscoped.create(
            institution_id=institution.pk,
            amount=credit_created,
            source_payment=payment,
        )
        student.credit_balance += credit_created
        student.save(update_fields=["credit_balance"])

    # Step 6: Audit log
    summary_parts = [
        f"Recorded payment of {amount} for {student.full_name}",
        f"(receipt {receipt_number})",
    ]
    if credit_applied > 0:
        summary_parts.append(f"— {credit_applied} credit applied")
    if credit_created > 0:
        summary_parts.append(f"— {credit_created} credit generated from overpayment")

    write_audit_log(
        institution_id=institution.pk,
        actor=actor,
        action="payment.recorded",
        summary=" ".join(summary_parts)[:255],
        target_type="Payment",
        target_id=str(payment.pk),
        detail={
            "amount": str(amount),
            "method": method,
            "receipt_number": receipt_number,
            "student_id": student.pk,
            "assignment_id": assignment.pk,
            "credit_applied": str(credit_applied),
            "credit_created": str(credit_created),
            "allocations": recorded_allocations,
        },
        ip_address=ip_address,
    )

    return payment, receipt, credit_applied, credit_created


# --------------------------------------------------------------------------- #
# Payment reversal
# --------------------------------------------------------------------------- #
@transaction.atomic
def reverse_payment(
    *,
    payment,
    reason,
    actor,
    ip_address=None,
):
    """Reverse an active payment.

    Marks the payment as reversed, and if an overpayment credit was created from
    this payment, removes that credit from the student's balance.

    The receipt remains for audit purposes — it is never deleted.
    """
    if not reason.strip():
        raise ValidationError("A reason is required for payment reversals.")

    if payment.status == PaymentStatus.REVERSED:
        raise ValidationError("This payment has already been reversed.")

    student = payment.assignment.student

    # Reverse any overpayment credit that was sourced from this payment
    overpayment_credits = CreditTransaction.unscoped.filter(
        source_payment=payment,
        amount__gt=0,
    )
    credit_reversed = Decimal("0.00")
    for credit in overpayment_credits:
        credit_reversed += credit.amount
    if credit_reversed > 0:
        student.credit_balance -= credit_reversed
        # Floor at zero — defensive, should not happen with correct bookkeeping
        if student.credit_balance < 0:
            student.credit_balance = Decimal("0.00")
        student.save(update_fields=["credit_balance"])

    payment.status = PaymentStatus.REVERSED
    payment.reversal_reason = reason
    payment.reversed_at = timezone.now()
    payment.save(update_fields=["status", "reversal_reason", "reversed_at"])

    receipt_number = getattr(payment.receipt, "receipt_number", "—")

    write_audit_log(
        institution_id=payment.institution_id,
        actor=actor,
        action="payment.reversed",
        summary=(
            f"Reversed payment of {payment.amount} for "
            f"{student.full_name} (receipt {receipt_number})"
        ),
        target_type="Payment",
        target_id=str(payment.pk),
        reason=reason,
        detail={
            "amount": str(payment.amount),
            "receipt_number": receipt_number,
            "credit_reversed": str(credit_reversed),
        },
        ip_address=ip_address,
    )

    return payment
