from decimal import Decimal
from django.db import migrations


def reconcile_reversed_credit_payments(apps, schema_editor):
    Payment = apps.get_model("billing", "Payment")
    CreditTransaction = apps.get_model("billing", "CreditTransaction")
    Student = apps.get_model("students", "Student")

    reversed_payments = Payment._base_manager.filter(status="reversed")

    for payment in reversed_payments:
        credit_used = Decimal("0.00")
        if payment.credit_applied and payment.credit_applied > Decimal("0.00"):
            credit_used = payment.credit_applied
        elif payment.method == "credit":
            credit_used = payment.amount

        if credit_used > Decimal("0.00") and payment.assignment_id:
            # Check if there is already a positive CreditTransaction on applied_to_assignment
            has_positive_offset = CreditTransaction._base_manager.filter(
                applied_to_assignment_id=payment.assignment_id,
                amount__gt=Decimal("0.00"),
            ).exists()

            if not has_positive_offset:
                # 1. Create offsetting positive CreditTransaction on the fee assignment
                CreditTransaction._base_manager.create(
                    institution_id=payment.institution_id,
                    amount=credit_used,
                    applied_to_assignment_id=payment.assignment_id,
                )

                # 2. Also remove any erroneous 0.00 CreditTransaction sourced from this reversal
                CreditTransaction._base_manager.filter(
                    source_payment=payment,
                    amount=Decimal("0.00"),
                ).delete()

                # 3. Restore student credit balance
                student = Student._base_manager.filter(pk=payment.assignment.student_id).first()
                if student:
                    student.credit_balance = (student.credit_balance or Decimal("0.00")) + credit_used
                    student.save(update_fields=["credit_balance"])


class Migration(migrations.Migration):

    dependencies = [
        ('billing', '0007_payment_credit_applied'),
    ]

    operations = [
        migrations.RunPython(reconcile_reversed_credit_payments, migrations.RunPython.noop),
    ]
