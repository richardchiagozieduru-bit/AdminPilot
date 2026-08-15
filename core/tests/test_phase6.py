"""
Phase 6 unit tests: Dashboard, Search, SearchSuggestView JSON endpoint, and Student Activity Timeline.
"""

import datetime
from decimal import Decimal

from django.urls import reverse

from accounts.models import User
from billing.models import PaymentMethod
from billing.services import create_fee_structure, record_payment
from core.tests.school import ApprovedSchoolTestCase


class Phase6DashboardAndSearchTests(ApprovedSchoolTestCase):
    def setUp(self):
        super().setUp()
        self.session, self.term, self.classes = self.configure_school()
        self.klass = self.classes[0]
        self.student = self.enroll_a_student(self.klass, self.session, self.term)
        self.sign_in_owner()

        with self.in_school():
            self.owner = User.objects.get(email=self.OWNER_EMAIL)
            self.structure = create_fee_structure(
                institution_id=self.institution.pk,
                name="First Term Tuition",
                klass=self.klass,
                session=self.session,
                term=self.term,
                items=[{"name": "Tuition", "amount": Decimal("50000.00")}],
                actor=self.owner,
            )

    def test_dashboard_context_computation_owner(self):
        with self.in_school():
            from billing.models import StudentFeeAssignment
            assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=self.structure
            )
            record_payment(
                assignment=assignment,
                amount=Decimal("20000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.CASH,
                actor=self.owner,
            )

        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["financials_available"])
        self.assertEqual(response.context["fees_due"], Decimal("50000.00"))
        self.assertEqual(response.context["fees_collected"], Decimal("20000.00"))
        self.assertEqual(response.context["received_today"], Decimal("20000.00"))
        self.assertEqual(response.context["student_count"], 1)
        self.assertEqual(response.context["class_count"], 2)
        self.assertEqual(len(response.context["recent_payments"]), 1)

    def test_dashboard_bursar_reduced_context(self):
        self.sign_in_as("Bursar")

        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        # Bursar gets financial widgets but NOT student_count or class_count
        self.assertTrue(response.context["financials_available"])
        self.assertNotIn("student_count", response.context)
        self.assertNotIn("class_count", response.context)

    def test_search_view_renders_results(self):
        response = self.client.get(reverse("core:search"), {"q": "Chidi"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Chidi Okeke")

    def test_search_suggest_json_endpoint(self):
        response = self.client.get(reverse("core:search_suggest"), {"q": "Chidi"})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("suggestions", data)
        self.assertTrue(len(data["suggestions"]) >= 1)
        self.assertEqual(data["suggestions"][0]["title"], "Chidi Okeke")
        self.assertEqual(data["suggestions"][0]["category"], "Student")

    def test_student_timeline_renders_events(self):
        with self.in_school():
            from billing.models import StudentFeeAssignment
            assignment = StudentFeeAssignment.unscoped.get(
                student=self.student, fee_structure=self.structure
            )
            record_payment(
                assignment=assignment,
                amount=Decimal("50000.00"),
                payment_date=datetime.date.today(),
                method=PaymentMethod.TRANSFER,
                actor=self.owner,
            )

        response = self.client.get(
            reverse("students:timeline", kwargs={"pk": self.student.pk})
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Enrolled into class")
        self.assertContains(response, "Payment Recorded")
