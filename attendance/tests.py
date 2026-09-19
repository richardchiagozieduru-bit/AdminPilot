"""
Tests for the Attendance Module: Models, Services, Views, Permissions, and Multi-tenancy.
"""

import datetime
from django.test import TestCase
from django.urls import reverse

from academic.models import Class, ClassStatus
from accounts.models import User
from core.middleware import institution_db_context
from core.models import AuditLog, Institution
from core.permissions import ADMINISTRATOR, BURSAR, OWNER, STAFF
from core.tests.school import ApprovedSchoolTestCase, PASSWORD
from students.models import Gender, Student, StudentEnrollment, StudentStatus

from .models import AttendanceRecord, AttendanceStatus, ClassAttendanceRegister
from .services import (
    clear_daily_attendance_for_class,
    get_class_monthly_matrix,
    get_student_attendance_summary,
    mark_all_present_for_class,
    save_daily_attendance_for_class,
)


class AttendanceTestCase(ApprovedSchoolTestCase):
    """Base setup for attendance tests: school configured with session, term, classes, and students."""

    def setUp(self):
        super().setUp()
        self.session, self.term, self.classes = self.configure_school(class_names=["JSS 1A", "JSS 1B"])
        self.class_1a = self.classes[0]
        self.class_1b = self.classes[1]

        with self.in_school():
            # Create form teacher for class 1A
            self.teacher_user = User.objects.create_user(
                email="teacher@sunrise.example",
                institution=self.institution,
                role=STAFF,
                full_name="John Teacher",
                password=PASSWORD,
                is_active=True,
            )
            self.class_1a.form_teacher = self.teacher_user
            self.class_1a.save()

            # Create another staff member (not form teacher for 1A)
            self.other_teacher = User.objects.create_user(
                email="other@sunrise.example",
                institution=self.institution,
                role=STAFF,
                full_name="Mary Other",
                password=PASSWORD,
                is_active=True,
            )

            # Create students enrolled in class 1A
            self.student1 = Student.objects.create(
                institution=self.institution,
                first_name="Chioma",
                last_name="Okeke",
                gender=Gender.FEMALE,
                date_of_birth=datetime.date(2014, 5, 12),
                admission_number="SR-2026-001",
                guardian_name="Amaka Okeke",
                guardian_phone="08031112222",
                status=StudentStatus.ACTIVE,
            )
            StudentEnrollment.objects.create(
                institution=self.institution,
                student=self.student1,
                klass=self.class_1a,
                session=self.session,
                term=self.term,
            )

            self.student2 = Student.objects.create(
                institution=self.institution,
                first_name="Emeka",
                last_name="Eze",
                gender=Gender.MALE,
                date_of_birth=datetime.date(2014, 8, 20),
                admission_number="SR-2026-002",
                guardian_name="Chidi Eze",
                guardian_phone="08033334444",
                status=StudentStatus.ACTIVE,
            )
            StudentEnrollment.objects.create(
                institution=self.institution,
                student=self.student2,
                klass=self.class_1a,
                session=self.session,
                term=self.term,
            )


class AttendanceServiceTests(AttendanceTestCase):
    def test_save_daily_attendance_and_totals(self):
        target_date = datetime.date(2026, 9, 14)  # A Monday
        attendance_data = [
            {"student_id": self.student1.pk, "status": AttendanceStatus.PRESENT, "remark": ""},
            {"student_id": self.student2.pk, "status": AttendanceStatus.ABSENT, "remark": "Fever"},
        ]

        with self.in_school():
            register = save_daily_attendance_for_class(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                date_obj=target_date,
                records_data=attendance_data,
                actor=self.teacher_user,
            )

            self.assertEqual(register.present_count, 1)
            self.assertEqual(register.absent_count, 1)
            self.assertEqual(register.total_students, 2)
            self.assertEqual(register.taken_by, self.teacher_user)

            records = AttendanceRecord.objects.filter(register=register).order_by("student__first_name")
            self.assertEqual(records.count(), 2)
            self.assertEqual(records[0].status, AttendanceStatus.PRESENT)
            self.assertEqual(records[1].status, AttendanceStatus.ABSENT)
            self.assertEqual(records[1].remark, "Fever")

            # Audit log was written
            audit = AuditLog.objects.filter(action="attendance.recorded", institution=self.institution).first()
            self.assertIsNotNone(audit)

    def test_mark_all_present(self):
        target_date = datetime.date(2026, 9, 15)  # Tuesday

        with self.in_school():
            register = mark_all_present_for_class(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                date_obj=target_date,
                actor=self.teacher_user,
            )

            self.assertEqual(register.present_count, 2)
            self.assertEqual(register.absent_count, 0)
            self.assertEqual(register.attendance_rate, 100.0)

            # Verify both student records are PRESENT
            rec1 = AttendanceRecord.objects.get(register=register, student=self.student1)
            rec2 = AttendanceRecord.objects.get(register=register, student=self.student2)
            self.assertEqual(rec1.status, AttendanceStatus.PRESENT)
            self.assertEqual(rec2.status, AttendanceStatus.PRESENT)

    def test_build_monthly_matrix(self):
        with self.in_school():
            # Mark attendance on one day
            save_daily_attendance_for_class(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                date_obj=datetime.date(2026, 9, 14),
                records_data=[
                    {"student_id": self.student1.pk, "status": AttendanceStatus.PRESENT, "remark": ""},
                    {"student_id": self.student2.pk, "status": AttendanceStatus.LATE, "remark": "Traffic"},
                ],
                actor=self.teacher_user,
            )

            matrix = get_class_monthly_matrix(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                year=2026,
                month=9,
            )

            self.assertIn("school_days", matrix)
            self.assertIn("students", matrix)
            self.assertIn("daily_totals_list", matrix)

            # No weekends in school_days
            for d in matrix["school_days"]:
                self.assertLess(d.weekday(), 5)

            # Check student row structure
            self.assertEqual(len(matrix["students"]), 2)
            chioma_row = next(r for r in matrix["students"] if r["student"].pk == self.student1.pk)
            self.assertEqual(chioma_row["totals"]["present"], 1)
            self.assertEqual(chioma_row["totals"]["rate"], 100.0)

            emeka_row = next(r for r in matrix["students"] if r["student"].pk == self.student2.pk)
            self.assertEqual(emeka_row["totals"]["late"], 1)

    def test_student_attendance_summary(self):
        with self.in_school():
            save_daily_attendance_for_class(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                date_obj=datetime.date(2026, 9, 14),
                records_data=[
                    {"student_id": self.student1.pk, "status": AttendanceStatus.PRESENT, "remark": ""},
                ],
                actor=self.teacher_user,
            )
            save_daily_attendance_for_class(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                date_obj=datetime.date(2026, 9, 15),
                records_data=[
                    {"student_id": self.student1.pk, "status": AttendanceStatus.ABSENT, "remark": "Sick"},
                ],
                actor=self.teacher_user,
            )

            summary = get_student_attendance_summary(student=self.student1, term=self.term)
            self.assertEqual(summary["total_days"], 2)
            self.assertEqual(summary["present"], 1)
            self.assertEqual(summary["absent"], 1)
            self.assertEqual(summary["rate"], 50.0)


class AttendanceViewTests(AttendanceTestCase):
    def test_owner_dashboard_access(self):
        self.sign_in_owner()
        response = self.client.get(reverse("attendance:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Attendance")
        self.assertContains(response, "JSS 1A")
        self.assertContains(response, "JSS 1B")

    def test_form_teacher_dashboard_shows_spotlight(self):
        self.sign_in_with("teacher@sunrise.example")
        response = self.client.get(reverse("attendance:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Class — JSS 1A")
        self.assertContains(response, "Open Register")

    def test_monthly_register_view_accessible(self):
        self.sign_in_with("teacher@sunrise.example")
        url = reverse("attendance:class_register", kwargs={"class_id": self.class_1a.pk})
        response = self.client.get(url + "?year=2026&month=9")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "JSS 1A — Attendance Register")
        self.assertContains(response, "Chioma Okeke")
        self.assertContains(response, "Emeka Eze")
        self.assertTrue(response.context["can_manage"])

    def test_unassigned_teacher_cannot_manage_other_class_register(self):
        self.sign_in_with("other@sunrise.example")
        url = reverse("attendance:class_register", kwargs={"class_id": self.class_1a.pk})
        response = self.client.get(url)
        # Unassigned staff has no access to another teacher's class register
        self.assertEqual(response.status_code, 403)

    def test_bursar_can_view_register_read_only(self):
        self.sign_in_as(BURSAR, "bursar@sunrise.example")
        url = reverse("attendance:class_register", kwargs={"class_id": self.class_1a.pk})
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage"])

    def test_save_daily_attendance_post_success(self):
        self.sign_in_with("teacher@sunrise.example")
        url = reverse("attendance:save_daily", kwargs={"class_id": self.class_1a.pk})
        post_data = {
            "date": "2026-09-16",
            f"student_{self.student1.pk}": "present",
            f"remark_{self.student1.pk}": "",
            f"student_{self.student2.pk}": "absent",
            f"remark_{self.student2.pk}": "Headache",
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 302)

        with self.in_school():
            register = ClassAttendanceRegister.objects.get(
                institution=self.institution,
                klass=self.class_1a,
                date=datetime.date(2026, 9, 16),
            )
            self.assertEqual(register.present_count, 1)
            self.assertEqual(register.absent_count, 1)

    def test_unassigned_teacher_save_attendance_forbidden(self):
        self.sign_in_with("other@sunrise.example")
        url = reverse("attendance:save_daily", kwargs={"class_id": self.class_1a.pk})
        post_data = {
            "date": "2026-09-16",
            f"student_{self.student1.pk}": "present",
        }
        response = self.client.post(url, post_data)
        self.assertEqual(response.status_code, 403)

    def test_mark_all_present_post_success(self):
        self.sign_in_with("teacher@sunrise.example")
        url = reverse("attendance:mark_all_present", kwargs={"class_id": self.class_1a.pk})
        response = self.client.post(url, {"date": "2026-09-17"})
        self.assertEqual(response.status_code, 302)

        with self.in_school():
            register = ClassAttendanceRegister.objects.get(
                institution=self.institution,
                klass=self.class_1a,
                date=datetime.date(2026, 9, 17),
            )
            self.assertEqual(register.present_count, 2)
            self.assertEqual(register.attendance_rate, 100.0)

    def test_export_csv(self):
        self.sign_in_owner()
        url = reverse("attendance:export_csv", kwargs={"class_id": self.class_1a.pk})
        response = self.client.get(url + "?year=2026&month=9")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode("utf-8")
        self.assertIn("Student Name", content)
        self.assertIn("Admission No.", content)
        self.assertIn("Chioma Okeke", content)
        self.assertIn("Emeka Eze", content)

    def test_clear_daily_attendance(self):
        target_date = datetime.date(2026, 9, 14)
        with self.in_school():
            # Mark all present first
            mark_all_present_for_class(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                date_obj=target_date,
                actor=self.teacher_user,
            )
            reg = ClassAttendanceRegister.objects.get(
                institution=self.institution, klass=self.class_1a, date=target_date
            )
            self.assertEqual(reg.present_count, 2)

            # Clear / unmark
            clear_daily_attendance_for_class(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                date_obj=target_date,
                actor=self.teacher_user,
            )
            reg.refresh_from_db()
            self.assertEqual(reg.present_count, 0)
            self.assertEqual(reg.total_students, 0)
            self.assertEqual(AttendanceRecord.objects.filter(register=reg).count(), 0)

    def test_unmark_all_post_success(self):
        self.sign_in_with("teacher@sunrise.example")
        target_date = datetime.date(2026, 9, 14)

        with self.in_school():
            mark_all_present_for_class(
                institution_id=self.institution.pk,
                klass=self.class_1a,
                date_obj=target_date,
                actor=self.teacher_user,
            )

        url = reverse("attendance:unmark_all", kwargs={"class_id": self.class_1a.pk})
        response = self.client.post(url, {"date": target_date.isoformat()})
        self.assertEqual(response.status_code, 302)

        with self.in_school():
            reg = ClassAttendanceRegister.objects.get(
                institution=self.institution, klass=self.class_1a, date=target_date
            )
            self.assertEqual(reg.present_count, 0)
            self.assertEqual(AttendanceRecord.objects.filter(register=reg).count(), 0)

    def test_record_attendance_for_previous_day(self):
        self.sign_in_with("teacher@sunrise.example")
        past_date = datetime.date(2026, 9, 8)  # Previous Tuesday

        # View register focused on past date
        url = reverse("attendance:class_register", kwargs={"class_id": self.class_1a.pk})
        response = self.client.get(f"{url}?year=2026&month=9&date={past_date.isoformat()}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["active_date"], past_date)

        # Save roll call for the past date
        post_url = reverse("attendance:save_daily", kwargs={"class_id": self.class_1a.pk})
        post_data = {
            "date": past_date.isoformat(),
            f"student_{self.student1.pk}": "present",
            f"student_{self.student2.pk}": "late",
            f"remark_{self.student2.pk}": "Dentist visit",
        }
        post_response = self.client.post(post_url, post_data)
        self.assertEqual(post_response.status_code, 302)

        with self.in_school():
            reg = ClassAttendanceRegister.objects.get(
                institution=self.institution, klass=self.class_1a, date=past_date
            )
            self.assertEqual(reg.present_count, 1)
            self.assertEqual(reg.late_count, 1)
            rec2 = AttendanceRecord.objects.get(register=reg, student=self.student2)
            self.assertEqual(rec2.status, AttendanceStatus.LATE)
            self.assertEqual(rec2.remark, "Dentist visit")

    def test_form_teacher_dashboard_redirects_to_class_register(self):
        """When an assigned Form Teacher visits `/`, they are redirected to their class register."""
        self.sign_in_with("teacher@sunrise.example")
        response = self.client.get("/")
        expected_url = reverse("attendance:class_register", kwargs={"class_id": self.class_1a.pk})
        self.assertRedirects(response, expected_url)

    def test_unassigned_teacher_dashboard_redirects_to_attendance_hub(self):
        """When an unassigned Staff visits `/`, they are redirected to `/attendance/`."""
        with self.in_school():
            unassigned_teacher = User.objects.create_user(
                institution=self.institution,
                email="newbie@sunrise.example",
                password="password123",
                role=User.Role.STAFF,
                full_name="Newbie Teacher",
                is_active=True,
            )
        self.sign_in_with("newbie@sunrise.example")
        response = self.client.get("/")
        expected_url = reverse("attendance:dashboard")
        self.assertRedirects(response, expected_url)

