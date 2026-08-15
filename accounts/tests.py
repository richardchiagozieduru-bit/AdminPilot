"""
Phase 2 exit conditions, registration, login, and Phase 8 User Management & Permission Matrix audit.
"""

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from accounts.services import (
    _derive_code,
    activate_invited_user,
    invite_user,
    register_institution,
    update_user_role_and_status,
)
from core.middleware import institution_db_context
from core.models import AuditLog, Institution
from core.tests.school import ApprovedSchoolTestCase
from platform_admin.models import PlatformUser


class RegistrationFormTests(TestCase):
    def test_registration_creates_pending_institution_and_inactive_owner(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "school_name": "Sunrise Academy",
                "school_type": Institution.Type.SECONDARY,
                "owner_name": "Ada Obi",
                "owner_email": "ada@sunrise.example",
                "owner_phone": "08030000000",
                "password1": "correct-horse-9",
                "password2": "correct-horse-9",
            },
        )
        self.assertRedirects(response, reverse("accounts:register_pending"))

        institution = Institution.objects.get(name="Sunrise Academy")
        self.assertEqual(institution.status, Institution.Status.PENDING)

        with institution_db_context(institution.pk):
            owner = User.objects.get(email="ada@sunrise.example")
        self.assertEqual(owner.role, User.Role.OWNER)
        self.assertFalse(
            owner.is_active,
            "The Owner must stay inactive until a Super Admin approves.",
        )

    def test_registration_rejects_duplicate_email(self):
        register_institution(
            school_name="First School",
            school_type=Institution.Type.PRIMARY,
            owner_name="Ada Obi",
            owner_email="ada@first.example",
            owner_phone="",
            password="correct-horse-9",
        )

        response = self.client.post(
            reverse("accounts:register"),
            {
                "school_name": "Second School",
                "school_type": Institution.Type.PRIMARY,
                "owner_name": "Ada Obi",
                "owner_email": "ada@first.example",
                "owner_phone": "",
                "password1": "correct-horse-9",
                "password2": "correct-horse-9",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already exists")
        self.assertEqual(Institution.objects.filter(name="Second School").count(), 0)

    def test_registration_rejects_mismatched_passwords(self):
        response = self.client.post(
            reverse("accounts:register"),
            {
                "school_name": "Mismatch School",
                "school_type": Institution.Type.OTHER,
                "owner_name": "Ada Obi",
                "owner_email": "ada@mismatch.example",
                "owner_phone": "",
                "password1": "correct-horse-9",
                "password2": "correct-horse-8",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "do not match")
        self.assertFalse(Institution.objects.filter(name="Mismatch School").exists())

    def test_pending_page_names_no_institution(self):
        response = self.client.get(reverse("accounts:register_pending"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("institution", response.context)


class DerivedCodeTests(TestCase):
    def test_initials_of_first_three_words(self):
        self.assertEqual(_derive_code("Sunrise Model Academy", set()), "SMA")

    def test_short_name_falls_back_to_letters(self):
        self.assertEqual(_derive_code("Eton", set()), "ETON")

    def test_collision_gets_a_numeric_suffix(self):
        self.assertEqual(_derive_code("Sunrise Model Academy", {"SMA"}), "SMA2")


class LoginGateTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.owners = {}
        for status in (
            Institution.Status.APPROVED,
            Institution.Status.PENDING,
            Institution.Status.REJECTED,
            Institution.Status.SUSPENDED,
        ):
            institution = register_institution(
                school_name=f"{status.label} School",
                school_type=Institution.Type.PRIMARY,
                owner_name="Ada Obi",
                owner_email=f"owner-{status.value}@example.test",
                owner_phone="",
                password="correct-horse-9",
            )
            institution.status = status
            institution.save(update_fields=["status"])

            if status == Institution.Status.APPROVED:
                with institution_db_context(institution.pk):
                    User.objects.filter(institution_id=institution.pk).update(
                        is_active=True
                    )
            cls.owners[status] = (institution, f"owner-{status.value}@example.test")

    def _login(self, status):
        _, email = self.owners[status]
        return self.client.post(
            reverse("accounts:login"),
            {"username": email, "password": "correct-horse-9"},
        )

    def test_approved_owner_can_log_in(self):
        response = self._login(Institution.Status.APPROVED)
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

    def test_pending_owner_is_told_the_school_is_under_review(self):
        response = self._login(Institution.Status.PENDING)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "still under review")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_rejected_owner_cannot_log_in(self):
        response = self._login(Institution.Status.REJECTED)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "was not approved")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_suspended_owner_cannot_log_in(self):
        response = self._login(Institution.Status.SUSPENDED)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "suspended")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_wrong_password_stays_generic(self):
        _, email = self.owners[Institution.Status.PENDING]
        response = self.client.post(
            reverse("accounts:login"), {"username": email, "password": "wrong-pass-1"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "still under review")

    def test_platform_account_cannot_use_the_institution_login(self):
        PlatformUser.objects.create_user(
            email="staff@adminpilot.test", password="correct-horse-9"
        )
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "staff@adminpilot.test", "password": "correct-horse-9"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_institution_account_cannot_use_the_platform_login(self):
        _, email = self.owners[Institution.Status.APPROVED]
        response = self.client.post(
            reverse("platform_admin:login"),
            {"username": email, "password": "correct-horse-9"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)


class AnonymousAccessTests(TestCase):
    def test_dashboard_redirects_anonymous_to_login(self):
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])

    def test_class_list_redirects_anonymous_to_login(self):
        response = self.client.get(reverse("academic:class_list"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response["Location"])


# --------------------------------------------------------------------------- #
# Phase 8: User Management, Invitation Setup Links & Permission Matrix Audit
# --------------------------------------------------------------------------- #
class UserManagementAndPermissionTests(ApprovedSchoolTestCase):
    def setUp(self):
        super().setUp()
        self.session, self.term, self.classes = self.configure_school()
        self.sign_in_owner()

        with self.in_school():
            self.owner = User.objects.get(email=self.OWNER_EMAIL)

    def test_owner_can_invite_bursar_and_bursar_can_accept_setup_link(self):
        with self.in_school():
            invited_user, uidb64, token = invite_user(
                institution_id=self.institution.pk,
                full_name="Bursar Person",
                email="bursar-invite@sunrise.example",
                role=User.Role.BURSAR,
                actor=self.owner,
            )
            self.assertFalse(invited_user.is_active)
            self.assertTrue(
                AuditLog.unscoped.filter(
                    institution_id=self.institution.pk,
                    action="user.invited",
                ).exists()
            )

        # Accept setup link as invited user (unauthenticated)
        self.client.logout()
        accept_url = reverse(
            "accounts:user_accept_invite",
            kwargs={"uidb64": uidb64, "token": token},
        )
        response = self.client.get(accept_url)
        self.assertEqual(response.status_code, 200)

        # Submit new password
        response = self.client.post(
            accept_url,
            {"password1": "new-secure-pass-123", "password2": "new-secure-pass-123"},
        )
        self.assertEqual(response.status_code, 302)

        # Verify active and logged in
        with self.in_school():
            invited_user.refresh_from_db()
            self.assertTrue(invited_user.is_active)
        self.assertIn("_auth_user_id", self.client.session)

    def test_owner_can_edit_user_role_and_disable(self):
        bursar = self.add_staff(User.Role.BURSAR, "bursar-edit@sunrise.example")

        # Edit user
        with self.in_school():
            update_user_role_and_status(
                user=bursar,
                role=User.Role.ADMINISTRATOR,
                is_active=False,
                actor=self.owner,
            )
            bursar.refresh_from_db()
            self.assertEqual(bursar.role, User.Role.ADMINISTRATOR)
            self.assertFalse(bursar.is_active)

        # Disabled user cannot log in
        self.client.logout()
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "bursar-edit@sunrise.example", "password": "correct-horse-9"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_non_owner_cannot_access_user_management(self):
        self.sign_in_as("Administrator")
        response = self.client.get(reverse("accounts:user_list"))
        self.assertEqual(response.status_code, 403)

        response = self.client.get(reverse("accounts:user_invite"))
        self.assertEqual(response.status_code, 403)

    def test_staff_role_has_no_access_to_any_module(self):
        self.sign_in_as("Staff")

        for url_name in [
            "core:dashboard",
            "academic:class_list",
            "students:list",
            "billing:fee_structure_list",
            "billing:payment_list",
            "reports:hub",
            "accounts:user_list",
            "core:institution_settings",
            "academic:structure",
        ]:
            response = self.client.get(reverse(url_name))
            self.assertEqual(
                response.status_code,
                403,
                f"Staff role must be denied access to {url_name}",
            )
