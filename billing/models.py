"""
Fees, payments, receipts, the credit ledger, and line-item allocations.

The core invariant (docs/02_Database.md): fee_structures.locked flips to true
the moment the first payment lands on any of its student assignments. After
that the only way to change an amount is a StudentFeeAssignment adjustment
(reason required, audit-logged) — never an edit of the shared structure.

Amounts are Decimal throughout; money is never stored as float.
"""

from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from academic.models import Class, Session, Term
from core.models import TenantScopedModel
from students.models import Student


class FeeStructure(TenantScopedModel):
    """A class-wide fee package for one session/term, with itemized lines."""

    name = models.CharField(max_length=100)
    klass = models.ForeignKey(
        Class, on_delete=models.PROTECT, related_name="fee_structures"
    )
    session = models.ForeignKey(
        Session, on_delete=models.PROTECT, related_name="fee_structures"
    )
    term = models.ForeignKey(
        Term, on_delete=models.PROTECT, related_name="fee_structures"
    )
    # Cached sum of items, recalculated on write only while unlocked — that
    # logic lives in the service layer (docs/02_Database.md CR-002).
    total_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    locked = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fee_structures"
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.name} ({self.klass.name})"


class FeeStructureItem(TenantScopedModel):
    """One line (tuition, PTA, exam fee, transport, ...) under a FeeStructure."""

    fee_structure = models.ForeignKey(
        FeeStructure, on_delete=models.CASCADE, related_name="items"
    )
    name = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=12, decimal_places=2)

    class Meta:
        db_table = "fee_structure_items"
        ordering = ("id",)

    def __str__(self):
        return f"{self.name} — {self.amount}"


class StudentFeeAssignment(TenantScopedModel):
    """The student-specific version of a FeeStructure.

    amount_due starts at the structure total and is adjusted per-student for
    waivers; adjustment_reason is required once it diverges (audit-logged by
    the service).
    """

    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="fee_assignments"
    )
    fee_structure = models.ForeignKey(
        FeeStructure, on_delete=models.PROTECT, related_name="assignments"
    )
    amount_due = models.DecimalField(max_digits=12, decimal_places=2)
    adjustment_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    adjusted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "student_fee_assignments"
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("student", "fee_structure"),
                name="ix_fee_assignment_student",
            )
        ]

    def __str__(self):
        try:
            student_str = f"{self.student.full_name} ({self.student.admission_number})"
        except Exception:
            student_str = f"Student #{self.student_id}"
        try:
            fee_str = f"{self.fee_structure.name} [{self.fee_structure.klass.name}]"
        except Exception:
            fee_str = f"Fee #{self.fee_structure_id}"
        return f"{student_str} — {fee_str}"

    @property
    def outstanding_balance(self):
        """docs/02_Database.md: amount_due − (payments − positive credit
        sourced from those payments) + applied_credits (which are negative).
        Read-only helper; the ledger remains the audit trail.

        Reversed payments are excluded — a reversal means the money is not
        counted against the debt, so the balance goes back up.
        """
        active_payments = self.payments.filter(status=PaymentStatus.ACTIVE)
        paid = active_payments.order_by().aggregate(total=models.Sum("amount"))[
            "total"
        ] or Decimal("0.00")
        credited_back = active_payments.order_by().aggregate(
            total=models.Sum("credit_transactions__amount")
        )["total"] or Decimal("0.00")
        applied_credits = self.applied_credits.order_by().aggregate(
            total=models.Sum("amount")
        )["total"] or Decimal("0.00")
        return self.amount_due - (paid - credited_back - applied_credits)

    def get_item_breakdown(self):
        """Returns a list of dicts for each FeeStructureItem under this assignment's
        fee structure, calculating how much has been paid to date, the remaining amount,
        and settlement status ('paid', 'partial', 'unpaid').
        """
        active_payments = self.payments.filter(status=PaymentStatus.ACTIVE)
        allocations = (
            PaymentItemAllocation.objects.filter(payment__in=active_payments)
            .order_by()
            .values("fee_item_id")
            .annotate(total_paid=models.Sum("amount"))
        )
        paid_map = {a["fee_item_id"]: a["total_paid"] for a in allocations}

        applied_credits = abs(
            self.applied_credits.order_by().aggregate(total=models.Sum("amount"))["total"]
            or Decimal("0.00")
        )
        total_allocated = sum(paid_map.values(), Decimal("0.00"))
        total_active_paid = (
            active_payments.order_by().aggregate(t=models.Sum("amount"))["t"]
            or Decimal("0.00")
        ) + applied_credits
        unallocated_paid = max(Decimal("0.00"), total_active_paid - total_allocated)

        breakdown = []
        remaining_unallocated = unallocated_paid
        for item in self.fee_structure.items.all():
            direct_paid = paid_map.get(item.pk, Decimal("0.00"))
            billed = item.amount

            # Waterfall fallback for legacy payments
            fallback_applied = Decimal("0.00")
            if remaining_unallocated > 0:
                needed = max(Decimal("0.00"), billed - direct_paid)
                fallback_applied = min(needed, remaining_unallocated)
                remaining_unallocated -= fallback_applied

            total_item_paid = direct_paid + fallback_applied
            remaining = max(Decimal("0.00"), billed - total_item_paid)

            if total_item_paid >= billed and billed > 0:
                status = "paid"
                status_label = "Paid in full"
                badge_class = "badge--success"
            elif total_item_paid > 0:
                status = "partial"
                pct = int((total_item_paid / billed) * 100) if billed > 0 else 0
                status_label = f"Partial ({pct}%)"
                badge_class = "badge--warning"
            else:
                status = "unpaid"
                status_label = "Unpaid"
                badge_class = "badge--danger"

            breakdown.append({
                "fee_item": item,
                "fee_item_id": item.pk,
                "name": item.name,
                "billed": billed,
                "paid": total_item_paid,
                "remaining": remaining,
                "status": status,
                "status_label": status_label,
                "badge_class": badge_class,
            })
        return breakdown


class PaymentMethod(models.TextChoices):
    CASH = "cash", "Cash"
    TRANSFER = "transfer", "Transfer"
    CARD = "card", "Card"
    POS = "pos", "POS"
    CHEQUE = "cheque", "Cheque"
    OTHER = "other", "Other"


class PaymentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    REVERSED = "reversed", "Reversed"


class Payment(TenantScopedModel):
    assignment = models.ForeignKey(
        StudentFeeAssignment, on_delete=models.PROTECT, related_name="payments"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_date = models.DateField()
    method = models.CharField(max_length=10, choices=PaymentMethod.choices)
    status = models.CharField(
        max_length=10, choices=PaymentStatus.choices, default=PaymentStatus.ACTIVE
    )
    reversal_reason = models.TextField(blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="recorded_payments",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments"
        ordering = ("-payment_date", "-created_at")

    def __str__(self):
        return f"{self.amount} on {self.payment_date}"


class PaymentItemAllocation(TenantScopedModel):
    """Line-item payment allocation (Custom Item Settlement).
    
    Tracks the exact portion of a payment allocated to a specific FeeStructureItem.
    """

    payment = models.ForeignKey(
        Payment, on_delete=models.CASCADE, related_name="allocations"
    )
    fee_item = models.ForeignKey(
        FeeStructureItem, on_delete=models.PROTECT, related_name="allocations"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payment_item_allocations"
        ordering = ("id",)

    def __str__(self):
        return f"{self.fee_item.name}: {self.amount} for Payment #{self.payment_id}"


class Receipt(TenantScopedModel):
    payment = models.OneToOneField(
        Payment, on_delete=models.PROTECT, related_name="receipt"
    )
    receipt_number = models.CharField(max_length=32)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="issued_receipts",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "receipts"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("institution", "receipt_number"),
                name="uniq_receipt_number",
            )
        ]


class CreditTransaction(TenantScopedModel):
    """Append-only credit ledger.

    A positive row is created when a payment exceeds its assignment's
    outstanding balance (linked via source_payment); a negative row when a
    Bursar manually applies existing credit to another assignment (linked via
    applied_to_assignment). Credit is never auto-applied (docs/02_Database.md).
    """

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    source_payment = models.ForeignKey(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="credit_transactions",
    )
    applied_to_assignment = models.ForeignKey(
        StudentFeeAssignment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_credits",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "credit_transactions"
        ordering = ("-created_at",)

    def clean(self):
        super().clean()
        if (self.source_payment_id is None) == (self.applied_to_assignment_id is None):
            raise ValidationError(
                "A credit transaction must be linked to exactly one of "
                "source_payment or applied_to_assignment."
            )
