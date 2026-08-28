"""
Phase 5: Fee & Payment Engine tests.

Covers:
  - FeeStructure creation, itemization, auto-assignment to enrolled students
  - FeeStructure updating & locking once a payment exists
  - StudentFeeAssignment adjustment (reason required, audit-logged)
  - Payment recording (atomic: receipt generation, fee locking, overpayment credit)
  - Payment reversal (reason required, credit reversal)
  - Permission matrix (Owner/Admin/Bursar allowed, Staff denied)
  - Tenant isolation across all billing models and views
"""

import datetime
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.urls import reverse

from accounts.models import User
from billing.models import (
    CreditTransaction,
    FeeStructure,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Receipt,
    StudentFeeAssignment,
)
from billing.services import (
    adjust_student_fee,
    create_fee_structure,
    delete_fee_structure,
    record_payment,
    reverse_payment,
    update_fee_structure,
)
from core.models import AuditLog
from core.tests.school import ApprovedSchoolTestCase


class FeeEngineServiceTests(ApprovedSchoolTestCase):
    def setUp(self):
        super().setUp()
        self.session, self.term, self.classes = self.configure_school()
        self.klass = self.classes[0]
        self.student = self.enroll_a_student(self.klass, self.session, self.term)
        with self.in_school():
            self.owner = User.objects.get(email=self.OWNER_EMAIL)

    def test_create_fee_structure_auto_assigns_students(self):
        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="Tuition Term 1",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[
                    {"name": "Tuition", "amount": Decimal("50000.00")},
                    {"name": "Sports", "amount": Decimal("5000.00")},
                ],
                actor=self.owner,
            )

            self.assertEqual(structure.total_amount, Decimal("55000.00"))
            self.assertFalse(structure.locked)

            # Verify auto-assigned to student
            assignment = StudentFeeAssignment.unscoped.get(
                institution_id=self.institution.pk,
                student=self.student,
                fee_structure=structure,
            )
            self.assertEqual(assignment.amount_due, Decimal("55000.00"))
            self.assertEqual(assignment.outstanding_balance, Decimal("55000.00"))

            # Audit log check
            self.assertTrue(
                AuditLog.unscoped.filter(
                    institution_id=self.institution.pk,
                    action="fee_structure.created",
                ).exists()
            )

    def test_update_fee_structure_updates_unadjusted_assignments(self):
        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="Tuition Term 1",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("50000.00")}],
                actor=self.owner,
            )

            update_fee_structure(
                fee_structure=structure,
                name="Tuition Term 1 Updated",
                items=[
                    {"name": "Tuition", "amount": Decimal("55000.00")},
                    {"name": "ICT", "amount": Decimal("5000.00")},
                ],
                actor=self.owner,
            )

            structure.refresh_from_db()
            self.assertEqual(structure.total_amount, Decimal("60000.00"))

            assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=structure
            )
            self.assertEqual(assignment.amount_due, Decimal("60000.00"))

    def test_fee_structure_locks_on_payment_and_allows_safe_update(self):
        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="Tuition",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("50000.00")}],
                actor=self.owner,
            )
            assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=structure
            )

            # Record a payment of 20,000
            record_payment(
                assignment=assignment,
                amount=Decimal("20000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.TRANSFER,
                actor=self.owner,
            )

            structure.refresh_from_db()
            self.assertTrue(structure.locked)

            # Updating structure should succeed and update assignment due amount
            updated = update_fee_structure(
                fee_structure=structure,
                name="Updated Tuition",
                items=[{"name": "Tuition", "amount": Decimal("60000.00")}],
                actor=self.owner,
            )
            self.assertEqual(updated.total_amount, Decimal("60000.00"))
            assignment.refresh_from_db()
            self.assertEqual(assignment.amount_due, Decimal("60000.00"))
            self.assertEqual(assignment.total_paid, Decimal("20000.00"))
            self.assertEqual(assignment.outstanding_balance, Decimal("40000.00"))

    def test_adjust_student_fee_requires_reason(self):
        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="Tuition",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("50000.00")}],
                actor=self.owner,
            )
            assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=structure
            )

            with self.assertRaises(ValidationError):
                adjust_student_fee(
                    assignment=assignment,
                    new_amount=Decimal("40000.00"),
                    reason="",
                    actor=self.owner,
                )

            # With valid reason
            adjust_student_fee(
                assignment=assignment,
                new_amount=Decimal("40000.00"),
                reason="Scholarship discount",
                actor=self.owner,
            )
            assignment.refresh_from_db()
            self.assertEqual(assignment.amount_due, Decimal("40000.00"))
            self.assertEqual(assignment.adjustment_reason, "Scholarship discount")
            self.assertTrue(
                AuditLog.unscoped.filter(
                    institution_id=self.institution.pk,
                    action="fee_assignment.adjusted",
                ).exists()
            )

    def test_record_payment_generates_receipt_and_handles_overpayment(self):
        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="Tuition",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("50000.00")}],
                actor=self.owner,
            )
            assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=structure
            )

            # Record overpayment of 60,000 on a 50,000 fee
            payment, receipt, applied, created = record_payment(
                assignment=assignment,
                amount=Decimal("60000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.CASH,
                actor=self.owner,
            )

            self.assertEqual(payment.amount, Decimal("60000.00"))
            self.assertEqual(applied, Decimal("0.00"))
            self.assertEqual(created, Decimal("10000.00"))

            # Receipt format check
            self.assertTrue(receipt.receipt_number.startswith(self.institution.code))

            # Student credit balance check
            self.student.refresh_from_db()
            self.assertEqual(self.student.credit_balance, Decimal("10000.00"))

    def test_apply_credit_towards_new_payment(self):
        with self.in_school():
            # Create first fee structure and overpay
            s1 = create_fee_structure(
                institution_id=self.institution.pk,
                name="Term 1 Fee",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("20000.00")}],
                actor=self.owner,
            )
            a1 = StudentFeeAssignment.unscoped.get(student=self.student, fee_structure=s1)
            record_payment(
                assignment=a1,
                amount=Decimal("30000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.CASH,
                actor=self.owner,
            )
            self.student.refresh_from_db()
            self.assertEqual(self.student.credit_balance, Decimal("10000.00"))

            # Create second fee structure
            s2 = create_fee_structure(
                institution_id=self.institution.pk,
                name="Term 2 Fee",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("25000.00")}],
                actor=self.owner,
            )
            a2 = StudentFeeAssignment.unscoped.get(student=self.student, fee_structure=s2)

            # Record payment of 15,000 applying 10,000 credit
            p2, r2, applied, created = record_payment(
                assignment=a2,
                amount=Decimal("15000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.POS,
                actor=self.owner,
                apply_credit=True,
            )

            self.assertEqual(applied, Decimal("10000.00"))
            self.assertEqual(created, Decimal("0.00"))
            a2.refresh_from_db()
            self.assertEqual(a2.outstanding_balance, Decimal("0.00"))

            self.student.refresh_from_db()
            self.assertEqual(self.student.credit_balance, Decimal("0.00"))

    def test_reverse_payment_reverts_credit_and_status(self):
        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="Tuition",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("50000.00")}],
                actor=self.owner,
            )
            assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=structure
            )
            payment, receipt, _, _ = record_payment(
                assignment=assignment,
                amount=Decimal("60000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.TRANSFER,
                actor=self.owner,
            )

            self.student.refresh_from_db()
            self.assertEqual(self.student.credit_balance, Decimal("10000.00"))

            # Reversing without reason fails
            with self.assertRaises(ValidationError):
                reverse_payment(
                    payment=payment,
                    reason="",
                    actor=self.owner,
                )

            # Reversing with reason
            reverse_payment(
                payment=payment,
                reason="Bounced transfer",
                actor=self.owner,
            )

            payment.refresh_from_db()
            self.assertEqual(payment.status, PaymentStatus.REVERSED)
            self.assertEqual(payment.reversal_reason, "Bounced transfer")

            self.student.refresh_from_db()
            self.assertEqual(self.student.credit_balance, Decimal("0.00"))


class BillingViewsAndPermissionsTests(ApprovedSchoolTestCase):
    def setUp(self):
        super().setUp()
        self.session, self.term, self.classes = self.configure_school()
        self.klass = self.classes[0]
        self.student = self.enroll_a_student(self.klass, self.session, self.term)
        self.sign_in_owner()

        with self.in_school():
            self.owner = User.objects.get(email=self.OWNER_EMAIL)
            self.fee_structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="First Term Fee",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("40000.00")}],
                actor=self.owner,
            )
            self.assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=self.fee_structure
            )

    def test_bursar_has_full_billing_access(self):
        self.sign_in_as("Bursar")

        # Fee structure list
        response = self.client.get(reverse("billing:fee_structure_list"))
        self.assertEqual(response.status_code, 200)

        # Create fee structure
        response = self.client.post(
            reverse("billing:fee_structure_create"),
            {
                "name": "Bursar Created Fee",
                "klass": self.klass.pk,
                "session": self.session.pk,
                "term": self.term.pk,
                "items-TOTAL_FORMS": "1",
                "items-INITIAL_FORMS": "0",
                "items-MIN_NUM_FORMS": "1",
                "items-MAX_NUM_FORMS": "1000",
                "items-0-name": "Uniform",
                "items-0-amount": "15000.00",
            },
        )
        self.assertEqual(response.status_code, 302)

        # Record payment
        response = self.client.post(
            reverse("billing:payment_create"),
            {
                "assignment": self.assignment.pk,
                "amount": "40000.00",
                "payment_date": "2026-09-10",
                "method": "cash",
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_staff_has_no_billing_access(self):
        self.sign_in_as("Staff")

        response = self.client.get(reverse("billing:fee_structure_list"))
        self.assertEqual(response.status_code, 403)

        response = self.client.get(reverse("billing:payment_list"))
        self.assertEqual(response.status_code, 403)

    def test_receipt_print_view_renders(self):
        with self.in_school():
            payment, receipt, _, _ = record_payment(
                assignment=self.assignment,
                amount=Decimal("40000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.CASH,
                actor=self.owner,
            )

        response = self.client.get(reverse("billing:receipt_detail", kwargs={"pk": receipt.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, receipt.receipt_number)
        self.assertContains(response, "Print receipt")

    def test_student_profile_payment_tab_renders_real_data(self):
        with self.in_school():
            record_payment(
                assignment=self.assignment,
                amount=Decimal("40000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.TRANSFER,
                actor=self.owner,
            )

        response = self.client.get(
            reverse("students:payments", kwargs={"pk": self.student.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "First Term Fee")
        self.assertContains(response, "40000.00")

    def test_custom_item_allocation_settlement(self):
        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                klass=self.klass,
                session=self.session,
                term=self.term,
                name="Complete Term Package",
                items=[
                    {"name": "School Fees", "amount": Decimal("20000.00")},
                    {"name": "PTA Meeting", "amount": Decimal("10000.00")},
                    {"name": "Exam Fee", "amount": Decimal("5000.00")},
                    {"name": "Inter-house Sports", "amount": Decimal("3000.00")},
                ],
                actor=self.owner,
            )
            items_by_name = {it.name: it for it in structure.items.all()}
            assignment = StudentFeeAssignment.objects.get(
                student=self.student, fee_structure=structure
            )

            # Custom item settlement: clear Exam (5k) + Sports (3k) + part of School Fees (17k) = 25k
            allocations = [
                {"fee_item_id": items_by_name["Exam Fee"].pk, "amount": Decimal("5000.00")},
                {"fee_item_id": items_by_name["Inter-house Sports"].pk, "amount": Decimal("3000.00")},
                {"fee_item_id": items_by_name["School Fees"].pk, "amount": Decimal("17000.00")},
            ]

            payment, receipt, _, _ = record_payment(
                assignment=assignment,
                amount=Decimal("25000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.TRANSFER,
                actor=self.owner,
                item_allocations=allocations,
            )

            # Check allocations created
            self.assertEqual(payment.allocations.count(), 3)
            self.assertEqual(assignment.outstanding_balance, Decimal("13000.00"))

            # Check item breakdown status
            breakdown = {b["name"]: b for b in assignment.get_item_breakdown()}
            self.assertEqual(breakdown["Exam Fee"]["status"], "paid")
            self.assertEqual(breakdown["Exam Fee"]["remaining"], Decimal("0.00"))
            self.assertEqual(breakdown["Inter-house Sports"]["status"], "paid")
            self.assertEqual(breakdown["Inter-house Sports"]["remaining"], Decimal("0.00"))
            self.assertEqual(breakdown["School Fees"]["status"], "partial")
            self.assertEqual(breakdown["School Fees"]["paid"], Decimal("17000.00"))
            self.assertEqual(breakdown["School Fees"]["remaining"], Decimal("3000.00"))
            self.assertEqual(breakdown["PTA Meeting"]["status"], "unpaid")
            self.assertEqual(breakdown["PTA Meeting"]["remaining"], Decimal("10000.00"))

            # Test Reversal restores all item balances cleanly
            reverse_payment(
                payment=payment,
                reason="Parent cheque bounced",
                actor=self.owner,
            )
            reverted_breakdown = {b["name"]: b for b in assignment.get_item_breakdown()}
            self.assertEqual(reverted_breakdown["Exam Fee"]["status"], "unpaid")
            self.assertEqual(reverted_breakdown["Exam Fee"]["remaining"], Decimal("5000.00"))
            self.assertEqual(reverted_breakdown["School Fees"]["status"], "unpaid")
            self.assertEqual(reverted_breakdown["School Fees"]["remaining"], Decimal("20000.00"))

    def test_fee_assignment_str_representation(self):
        with self.in_school():
            assignment_str = str(self.assignment)
            self.assertIn(self.student.full_name, assignment_str)
            self.assertIn(self.fee_structure.name, assignment_str)
            self.assertNotIn("StudentFeeAssignment object", assignment_str)

    def test_delete_unlocked_fee_structure(self):
        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                klass=self.klass,
                session=self.session,
                term=self.term,
                name="Temporary Package",
                items=[{"name": "Books", "amount": Decimal("5000.00")}],
                actor=self.owner,
            )
            structure_pk = structure.pk
            self.assertTrue(FeeStructure.objects.filter(pk=structure_pk).exists())
            self.assertTrue(StudentFeeAssignment.objects.filter(fee_structure_id=structure_pk).exists())

            # Delete fee structure
            delete_fee_structure(
                fee_structure=structure,
                actor=self.owner,
            )

            # Check deleted
            self.assertFalse(FeeStructure.objects.filter(pk=structure_pk).exists())
            self.assertFalse(StudentFeeAssignment.objects.filter(fee_structure_id=structure_pk).exists())

    def test_delete_locked_fee_structure_succeeds_and_purges_payments(self):
        with self.in_school():
            # Record payment to lock self.fee_structure
            payment, receipt, _, _ = record_payment(
                assignment=self.assignment,
                amount=Decimal("10000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.CASH,
                actor=self.owner,
            )
            self.fee_structure.refresh_from_db()
            self.assertTrue(self.fee_structure.locked)
            self.assertEqual(Payment.objects.filter(assignment=self.assignment).count(), 1)

            structure_pk = self.fee_structure.pk
            delete_fee_structure(
                fee_structure=self.fee_structure,
                actor=self.owner,
            )

            self.assertFalse(FeeStructure.objects.filter(pk=structure_pk).exists())
            self.assertFalse(StudentFeeAssignment.objects.filter(fee_structure_id=structure_pk).exists())
            self.assertEqual(Payment.objects.filter(pk=payment.pk).count(), 0)
            self.assertEqual(Receipt.objects.filter(pk=receipt.pk).count(), 0)

    def test_payment_create_view_preselects_assignment(self):
        self.sign_in_owner()
        response = self.client.get(
            reverse("billing:payment_create") + f"?assignment_id={self.assignment.pk}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, str(self.assignment.pk))


