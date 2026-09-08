from decimal import Decimal
from django.core.management.base import BaseCommand
from django.db import models, transaction
from django.db.models import Q

from core.models import Institution
from core.middleware import set_database_session_context
from billing.models import StudentFeeAssignment, CreditTransaction, PaymentStatus
from students.models import Student


class Command(BaseCommand):
    help = (
        "Reconciles historical overpayments and synchronizes student credit balances. "
        "Finds fee assignments with negative raw balances (uncredited overpayments), "
        "creates CreditTransaction rows, and synchronizes student.credit_balance."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--institution-id",
            type=int,
            default=None,
            help="Limit reconciliation to a specific institution ID.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview changes without modifying the database.",
        )

    def handle(self, *args, **options):
        institution_id = options.get("institution_id")
        dry_run = options.get("dry_run", False)

        if dry_run:
            self.stdout.write(self.style.WARNING("=== DRY RUN MODE: No database changes will be saved ==="))

        institutions = Institution.objects.all()
        if institution_id:
            institutions = institutions.filter(pk=institution_id)

        total_credits_created = 0
        total_amount_credited = Decimal("0.00")
        total_students_synced = 0

        for inst in institutions:
            self.stdout.write(f"\nProcessing Institution: {inst.name} (ID={inst.pk}, Code={inst.code})...")
            set_database_session_context(inst.pk)

            assignments = StudentFeeAssignment.objects.select_related("student", "fee_structure")
            for assignment in assignments:
                raw = assignment.raw_balance
                if raw < Decimal("0.00"):
                    excess = abs(raw)
                    latest_payment = (
                        assignment.payments.filter(status=PaymentStatus.ACTIVE)
                        .order_by("-payment_date", "-id")
                        .first()
                    )

                    if not latest_payment:
                        self.stdout.write(
                            self.style.WARNING(
                                f"  [SKIP] Assignment #{assignment.pk} ({assignment.student.full_name}) has raw_balance {raw} "
                                "but no active payment found."
                            )
                        )
                        continue

                    self.stdout.write(
                        self.style.SUCCESS(
                            f"  [OVERPAYMENT DETECTED] Assignment #{assignment.pk} | "
                            f"Student: {assignment.student.full_name} ({assignment.student.admission_number}) | "
                            f"Overpaid: ₦{excess} | Linked to Payment #{latest_payment.pk}"
                        )
                    )

                    if not dry_run:
                        with transaction.atomic():
                            CreditTransaction.unscoped.create(
                                institution_id=inst.pk,
                                amount=excess,
                                source_payment=latest_payment,
                            )
                            student = assignment.student
                            student.credit_balance += excess
                            student.save(update_fields=["credit_balance"])

                    total_credits_created += 1
                    total_amount_credited += excess

            # Step 2: Resynchronize student credit balances with the CreditTransaction ledger
            students = Student.objects.all()
            for student in students:
                tx_sum = (
                    CreditTransaction.unscoped.filter(
                        Q(source_payment__assignment__student=student)
                        | Q(applied_to_assignment__student=student)
                    ).aggregate(total=models.Sum("amount"))["total"]
                    or Decimal("0.00")
                )
                expected_balance = max(Decimal("0.00"), tx_sum)

                if student.credit_balance != expected_balance:
                    self.stdout.write(
                        self.style.WARNING(
                            f"  [SYNC] Student #{student.pk} ({student.full_name}): "
                            f"credit_balance {student.credit_balance} -> {expected_balance}"
                        )
                    )
                    if not dry_run:
                        student.credit_balance = expected_balance
                        student.save(update_fields=["credit_balance"])
                    total_students_synced += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"\nReconciliation Complete:\n"
                f"  - Overpayment Credit Transactions Created: {total_credits_created} (Total: ₦{total_amount_credited})\n"
                f"  - Student Credit Balances Synchronized: {total_students_synced}"
            )
        )
