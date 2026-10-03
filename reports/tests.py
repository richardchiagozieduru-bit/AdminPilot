"""
Phase 7 unit tests: Reports calculation services, timezone date boundaries, CSV exports,
Owner data export endpoints, and permission gating.
"""

import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.urls import reverse

from accounts.models import User
from billing.models import PaymentMethod
from billing.services import create_fee_structure, record_payment
from core.models import AuditLog
from core.tests.school import ApprovedSchoolTestCase
from reports.services import (
    get_class_summary_data,
    get_income_report_data,
    get_outstanding_fees_data,
    get_student_payment_history_data,
)


class Phase7ReportsTests(ApprovedSchoolTestCase):
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
                name="Term 1 Tuition",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("50000.00")}],
                actor=self.owner,
            )
            from billing.models import StudentFeeAssignment
            self.assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=self.fee_structure
            )
            self.payment, self.receipt, _, _ = record_payment(
                assignment=self.assignment,
                amount=Decimal("30000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.TRANSFER,
                actor=self.owner,
            )

    def test_income_report_service(self):
        with self.in_school():
            data = get_income_report_data(
                institution_id=self.institution.pk,
                period="term",
            )
            self.assertEqual(data["total_collected"], Decimal("30000.00"))
            self.assertEqual(data["transfer_total"], Decimal("30000.00"))
            self.assertEqual(data["cash_total"], Decimal("0.00"))

    def test_outstanding_fees_service(self):
        with self.in_school():
            data = get_outstanding_fees_data(
                institution_id=self.institution.pk,
            )
            self.assertEqual(len(data["assignments"]), 1)
            self.assertEqual(data["total_outstanding"], Decimal("20000.00"))

    def test_student_payment_history_service(self):
        with self.in_school():
            data = get_student_payment_history_data(
                institution_id=self.institution.pk,
                student_id=self.student.pk,
            )
            self.assertEqual(data["total_billed"], Decimal("50000.00"))
            self.assertEqual(data["total_paid"], Decimal("30000.00"))
            self.assertEqual(data["total_outstanding"], Decimal("20000.00"))

    def test_class_summary_service(self):
        with self.in_school():
            data = get_class_summary_data(
                institution_id=self.institution.pk,
            )
            self.assertEqual(data["grand_billed"], Decimal("50000.00"))
            self.assertEqual(data["grand_collected"], Decimal("30000.00"))
            self.assertEqual(data["grand_outstanding"], Decimal("20000.00"))

    def test_reports_views_rendering(self):
        # Hub
        response = self.client.get(reverse("reports:hub"))
        self.assertEqual(response.status_code, 200)

        # Income
        response = self.client.get(reverse("reports:income"))
        self.assertEqual(response.status_code, 200)

        # Outstanding Fees
        response = self.client.get(reverse("reports:outstanding_fees"))
        self.assertEqual(response.status_code, 200)

        # Student Payment History
        response = self.client.get(
            reverse("reports:student_payment_history", kwargs={"pk": self.student.pk})
        )
        self.assertEqual(response.status_code, 200)

        # Class Summary
        response = self.client.get(reverse("reports:class_summary"))
        self.assertEqual(response.status_code, 200)

    def test_csv_export_views(self):
        response = self.client.get(reverse("reports:income_export"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment; filename=", response["Content-Disposition"])
        content = response.content.decode("utf-8")
        self.assertTrue(content.startswith("\ufeff"))
        self.assertIn("Income Report", content)

    def test_owner_data_exports_and_audit_log(self):
        # Student export
        response = self.client.get(reverse("reports:export_students"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))

        # Payment export
        response = self.client.get(reverse("reports:export_payments"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))

        with self.in_school():
            self.assertEqual(
                AuditLog.unscoped.filter(
                    institution_id=self.institution.pk,
                    action="data.exported",
                ).count(),
                2,
            )

    def test_bursar_denied_data_export(self):
        self.sign_in_as("Bursar")

        response = self.client.get(reverse("reports:export_students"))
        self.assertEqual(response.status_code, 403)

        response = self.client.get(reverse("reports:export_payments"))
        self.assertEqual(response.status_code, 403)

    def test_fee_intelligence_view_and_calculations(self):
        from billing.services import create_fee_structure, record_payment
        from billing.models import PaymentMethod, StudentFeeAssignment

        with self.in_school():
            structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="Second Term Senior Package",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[
                    {"name": "Tuition", "amount": Decimal("50000.00"), "is_mandatory": True},
                    {"name": "Books", "amount": Decimal("15000.00"), "is_mandatory": True},
                ],
                actor=self.owner,
            )
            assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=structure
            )
            # Pay 50,000 which waterfall allocates to Tuition (50k) and leaves Books unpaid (15k)
            record_payment(
                assignment=assignment,
                amount=Decimal("50000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.TRANSFER,
                actor=self.owner,
            )

        self.sign_in_owner()
        # Default landing with no params
        resp_default = self.client.get(reverse("reports:fee_intelligence"))
        self.assertEqual(resp_default.status_code, 200)
        self.assertContains(resp_default, "Fee Intelligence & Payment Analytics")

        # Query Tuition
        resp = self.client.get(reverse("reports:fee_intelligence"), {"fee_item_name": "Tuition"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Fee Intelligence & Payment Analytics")
        self.assertContains(resp, "Tuition")
        self.assertContains(resp, "Paid in Full")
        self.assertContains(resp, "50000.00")

        # Query Books
        resp_books = self.client.get(reverse("reports:fee_intelligence"), {"fee_item_name": "Books"})
        self.assertEqual(resp_books.status_code, 200)
        self.assertContains(resp_books, "Unpaid")
        self.assertContains(resp_books, "15000.00")

    def test_fee_intelligence_csv_export(self):
        self.sign_in_owner()
        resp = self.client.get(
            reverse("reports:fee_intelligence_export"),
            {"fee_item_name": "Tuition"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment; filename=", resp["Content-Disposition"])

    def test_bursar_allowed_fee_intelligence(self):
        self.sign_in_as("Bursar")
        resp = self.client.get(reverse("reports:fee_intelligence"))
        self.assertEqual(resp.status_code, 200)

