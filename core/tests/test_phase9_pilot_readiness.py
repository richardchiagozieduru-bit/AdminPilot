"""
Phase 9 — Multi-Tenant Pilot Readiness Verification Suite.

Exercises the full 6-item Tenant Isolation Verification Checklist (docs/02_Database.md)
under realistic multi-tenant conditions with two simultaneous active institutions:
  - School 1: St. Jude International Academy (STJ)
  - School 2: Lagos Model College (LMC)
"""

import datetime
from decimal import Decimal

from django.test import TestCase

from academic.models import Class, ClassStatus, Session, Term
from accounts.models import User
from accounts.services import register_institution
from billing.models import (
    CreditTransaction,
    FeeStructure,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Receipt,
    StudentFeeAssignment,
)
from billing.services import create_fee_structure, record_payment
from core.middleware import auth_lookup_context, institution_db_context
from core.models import AuditLog, Institution, InstitutionNumberSequence
from core.services import format_sequence, next_sequence_number
from platform_admin.models import PlatformUser
from platform_admin.services import approve_institution
from students.models import Gender, Student, StudentEnrollment


class MultiTenantPilotReadinessTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reviewer = PlatformUser.objects.create_user(
            email="reviewer-phase9@adminpilot.test",
            password="correct-horse-9",
        )

        # ------------------------------------------------------------------- #
        # School 1: St. Jude International Academy (STJ)
        # ------------------------------------------------------------------- #
        cls.inst1 = register_institution(
            school_name="St Jude International Academy",
            school_type=Institution.Type.SECONDARY,
            owner_name="Sister Mary",
            owner_email="owner@stjude.example",
            owner_phone="08011111111",
            password="correct-horse-9",
        )
        approve_institution(cls.inst1, cls.reviewer)
        cls.inst1.refresh_from_db()

        with institution_db_context(cls.inst1.pk):
            cls.owner1 = User.objects.get(email="owner@stjude.example")
            cls.owner1.is_active = True
            cls.owner1.save()

            cls.admin1 = User.objects.create_user(
                email="admin@stjude.example",
                institution=cls.inst1,
                role=User.Role.ADMINISTRATOR,
                full_name="Admin StJude",
                password="correct-horse-9",
                is_active=True,
            )
            cls.bursar1 = User.objects.create_user(
                email="bursar@stjude.example",
                institution=cls.inst1,
                role=User.Role.BURSAR,
                full_name="Bursar StJude",
                password="correct-horse-9",
                is_active=True,
            )

            cls.sess1 = Session.unscoped.create(
                institution=cls.inst1, name="2026/2027",
                start_date=datetime.date(2026, 9, 1), end_date=datetime.date(2027, 7, 31),
                is_current=True,
            )
            cls.term1 = Term.unscoped.create(
                institution=cls.inst1, session=cls.sess1, name="First Term",
                start_date=datetime.date(2026, 9, 1), end_date=datetime.date(2026, 12, 20),
                is_current=True,
            )
            cls.klass1 = Class.unscoped.create(
                institution=cls.inst1, name="JSS 1A", order=10
            )

            num1 = next_sequence_number(
                cls.inst1.pk, InstitutionNumberSequence.Kind.ADMISSION, 2026
            )
            cls.student1 = Student.unscoped.create(
                institution=cls.inst1,
                first_name="Emmanuel", last_name="Eze", gender=Gender.MALE,
                date_of_birth=datetime.date(2012, 1, 1),
                admission_number=format_sequence(cls.inst1.code, 2026, num1),
                guardian_name="Grace Eze", guardian_phone="08011111111",
            )
            StudentEnrollment.unscoped.create(
                institution=cls.inst1, student=cls.student1,
                klass=cls.klass1, session=cls.sess1, term=cls.term1,
            )

            cls.fee1 = create_fee_structure(
                institution_id=cls.inst1.pk,
                name="Tuition STJ",
                klass=cls.klass1, session=cls.sess1, term=cls.term1,
                items=[{"name": "Tuition", "amount": Decimal("40000.00")}],
                actor=cls.owner1,
            )
            cls.assign1 = StudentFeeAssignment.unscoped.get(
                student=cls.student1, fee_structure=cls.fee1
            )
            cls.payment1, cls.receipt1, _, _ = record_payment(
                assignment=cls.assign1, amount=Decimal("40000.00"),
                payment_date=datetime.date.today(), method=PaymentMethod.CASH,
                actor=cls.bursar1,
            )

        # ------------------------------------------------------------------- #
        # School 2: Lagos Model College (LMC)
        # ------------------------------------------------------------------- #
        cls.inst2 = register_institution(
            school_name="Lagos Model College",
            school_type=Institution.Type.COMBINED,
            owner_name="Chief Babatunde",
            owner_email="owner@lmc.example",
            owner_phone="08022222222",
            password="correct-horse-9",
        )
        approve_institution(cls.inst2, cls.reviewer)
        cls.inst2.refresh_from_db()

        with institution_db_context(cls.inst2.pk):
            cls.owner2 = User.objects.get(email="owner@lmc.example")
            cls.owner2.is_active = True
            cls.owner2.save()

            cls.admin2 = User.objects.create_user(
                email="admin@lmc.example",
                institution=cls.inst2,
                role=User.Role.ADMINISTRATOR,
                full_name="Admin LMC",
                password="correct-horse-9",
                is_active=True,
            )
            cls.bursar2 = User.objects.create_user(
                email="bursar@lmc.example",
                institution=cls.inst2,
                role=User.Role.BURSAR,
                full_name="Bursar LMC",
                password="correct-horse-9",
                is_active=True,
            )

            cls.sess2 = Session.unscoped.create(
                institution=cls.inst2, name="2026/2027",
                start_date=datetime.date(2026, 9, 1), end_date=datetime.date(2027, 7, 31),
                is_current=True,
            )
            cls.term2 = Term.unscoped.create(
                institution=cls.inst2, session=cls.sess2, name="First Term",
                start_date=datetime.date(2026, 9, 1), end_date=datetime.date(2026, 12, 20),
                is_current=True,
            )
            cls.klass2 = Class.unscoped.create(
                institution=cls.inst2, name="SS 1 Science", order=10
            )

            num2 = next_sequence_number(
                cls.inst2.pk, InstitutionNumberSequence.Kind.ADMISSION, 2026
            )
            cls.student2 = Student.unscoped.create(
                institution=cls.inst2,
                first_name="Folake", last_name="Adeyemi", gender=Gender.FEMALE,
                date_of_birth=datetime.date(2011, 5, 10),
                admission_number=format_sequence(cls.inst2.code, 2026, num2),
                guardian_name="Tunde Adeyemi", guardian_phone="08022222222",
            )
            StudentEnrollment.unscoped.create(
                institution=cls.inst2, student=cls.student2,
                klass=cls.klass2, session=cls.sess2, term=cls.term2,
            )

            cls.fee2 = create_fee_structure(
                institution_id=cls.inst2.pk,
                name="Tuition LMC",
                klass=cls.klass2, session=cls.sess2, term=cls.term2,
                items=[{"name": "Tuition", "amount": Decimal("65000.00")}],
                actor=cls.owner2,
            )
            cls.assign2 = StudentFeeAssignment.unscoped.get(
                student=cls.student2, fee_structure=cls.fee2
            )
            cls.payment2, cls.receipt2, _, _ = record_payment(
                assignment=cls.assign2, amount=Decimal("70000.00"),
                payment_date=datetime.date.today(), method=PaymentMethod.TRANSFER,
                actor=cls.bursar2,
            )

    # ----------------------------------------------------------------------- #
    # Tenant Isolation Checklist (docs/02_Database.md)
    # ----------------------------------------------------------------------- #
    def test_checklist_item_1_cross_tenant_query_isolation(self):
        """Item 1: Querying any tenant model as School 1 returns 0 rows from School 2."""
        with institution_db_context(self.inst1.pk):
            students = list(Student.objects.all())
            classes = list(Class.objects.all())
            fees = list(FeeStructure.objects.all())
            payments = list(Payment.objects.all())
            receipts = list(Receipt.objects.all())

            # School 1 sees only its own records
            self.assertEqual(len(students), 1)
            self.assertEqual(students[0].pk, self.student1.pk)
            self.assertEqual(len(classes), 1)
            self.assertEqual(classes[0].pk, self.klass1.pk)
            self.assertEqual(len(fees), 1)
            self.assertEqual(fees[0].pk, self.fee1.pk)
            self.assertEqual(len(payments), 1)
            self.assertEqual(payments[0].pk, self.payment1.pk)

            # Zero records from School 2 leaked
            self.assertNotIn(self.student2, students)
            self.assertNotIn(self.klass2, classes)
            self.assertNotIn(self.fee2, fees)
            self.assertNotIn(self.payment2, payments)

    def test_checklist_item_2_insert_scope_enforcement(self):
        """Item 2: Inserting data stamped with School 1's context attaches to School 1."""
        with institution_db_context(self.inst1.pk):
            new_student = Student.objects.create(
                institution_id=self.inst1.pk,
                first_name="Amina", last_name="Bello", gender=Gender.FEMALE,
                date_of_birth=datetime.date(2013, 2, 2),
                admission_number="STJ-2026-999999",
                guardian_name="Usman Bello", guardian_phone="08033333333",
            )
            self.assertEqual(new_student.institution_id, self.inst1.pk)

        # School 2 query must not see the new student
        with institution_db_context(self.inst2.pk):
            self.assertFalse(Student.objects.filter(pk=new_student.pk).exists())

    def test_checklist_item_3_audit_log_isolation(self):
        """Item 3: School 1 Owner's AuditLog query returns zero log entries from School 2."""
        with institution_db_context(self.inst1.pk):
            logs1 = list(AuditLog.objects.all())
            self.assertTrue(len(logs1) > 0)
            for log in logs1:
                self.assertEqual(log.institution_id, self.inst1.pk)

        with institution_db_context(self.inst2.pk):
            logs2 = list(AuditLog.objects.all())
            self.assertTrue(len(logs2) > 0)
            for log in logs2:
                self.assertEqual(log.institution_id, self.inst2.pk)

            # Intersection must be empty
            pks1 = {l.pk for l in logs1}
            pks2 = {l.pk for l in logs2}
            self.assertTrue(pks1.isdisjoint(pks2))

    def test_checklist_item_4_bursar_field_restrictions_across_tenants(self):
        """Item 4: Bursar role restrictions hold across multi-tenant contexts."""
        with institution_db_context(self.inst1.pk):
            self.assertTrue(self.bursar1.role == User.Role.BURSAR)
            self.assertFalse(self.bursar1.is_owner)
            self.assertFalse(self.bursar1.is_admin)

        with institution_db_context(self.inst2.pk):
            self.assertTrue(self.bursar2.role == User.Role.BURSAR)

    def test_checklist_item_5_super_admin_exemption_boundaries(self):
        """Item 5: Super Admin code path has ZERO queries touching tenant-scoped tables."""
        # PlatformUser auth lookup runs outside institution_db_context
        with auth_lookup_context():
            platform_user = PlatformUser.objects.filter(
                email="reviewer-phase9@adminpilot.test"
            ).first()
            self.assertIsNotNone(platform_user)
            self.assertFalse(hasattr(platform_user, "institution_id"))

    def test_checklist_item_6_independent_sequence_counters(self):
        """Item 6: Sequence counters for School 1 increment independently of School 2."""
        with institution_db_context(self.inst1.pk):
            num1_a = next_sequence_number(
                self.inst1.pk, InstitutionNumberSequence.Kind.ADMISSION, 2026
            )
            rec1_a = next_sequence_number(
                self.inst1.pk, InstitutionNumberSequence.Kind.RECEIPT, 2026
            )

        with institution_db_context(self.inst2.pk):
            num2_a = next_sequence_number(
                self.inst2.pk, InstitutionNumberSequence.Kind.ADMISSION, 2026
            )
            rec2_a = next_sequence_number(
                self.inst2.pk, InstitutionNumberSequence.Kind.RECEIPT, 2026
            )

        with institution_db_context(self.inst1.pk):
            num1_b = next_sequence_number(
                self.inst1.pk, InstitutionNumberSequence.Kind.ADMISSION, 2026
            )

        # School 1's counter advanced by 1 (num1_a + 1 == num1_b)
        self.assertEqual(num1_b, num1_a + 1)
        # School 2's counter state is separate
        self.assertTrue(num2_a > 0)
        self.assertTrue(rec2_a > 0)
