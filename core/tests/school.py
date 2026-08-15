"""
Shared test fixture: one approved school, configured or not.

Not a test module — deliberately named so the runner does not collect it. It
holds the setup that both core's and academic's suites need, because the
alternative is two copies of the same twenty lines drifting apart, and a fixture
that differs between apps is a fixture that hides bugs in one of them.

`institution_db_context` appears throughout. A test method is not a request, so
nothing has stamped SESSION_CONTEXT, and every write here would be refused by the
BLOCK predicate without it — see core/middleware.py.
"""

import datetime

from django.test import TestCase
from django.urls import reverse

from academic.models import Class, Session, Term
from accounts.models import User
from accounts.services import register_institution
from core.middleware import institution_db_context
from core.models import Institution
from platform_admin.models import PlatformUser
from platform_admin.services import approve_institution

PASSWORD = "correct-horse-9"

SESSION_START = datetime.date(2026, 9, 7)
SESSION_END = datetime.date(2027, 7, 23)
TERM_START = datetime.date(2026, 9, 7)
TERM_END = datetime.date(2026, 12, 18)


class ApprovedSchoolTestCase(TestCase):
    """A school sitting exactly on the Phase 2 / Phase 3 boundary.

    Approved, Owner active, and nothing configured — no session, no term, no
    class. That is the state a real school is in the first time it signs in, and
    the state every Phase 3 screen has to cope with. Call configure_school() in a
    test that needs the school past setup.
    """

    OWNER_EMAIL = "ada@sunrise.example"
    SCHOOL_NAME = "Sunrise Academy"

    @classmethod
    def setUpTestData(cls):
        cls.reviewer = PlatformUser.objects.create_user(
            email="reviewer@adminpilot.test",
            password=PASSWORD,
            full_name="Platform Reviewer",
        )
        cls.institution = register_institution(
            school_name=cls.SCHOOL_NAME,
            school_type=Institution.Type.SECONDARY,
            owner_name="Ada Obi",
            owner_email=cls.OWNER_EMAIL,
            owner_phone="08030000000",
            password=PASSWORD,
        )
        approve_institution(cls.institution, cls.reviewer)
        cls.institution.refresh_from_db()

    def in_school(self):
        """SESSION_CONTEXT for the length of a block, for reads *and* writes.

        Needed more often than it looks. A test method is not a request, and
        TenantContextMiddleware clears the stamp in its `finally` — so the moment
        `self.client.post(...)` returns, the connection is back to "no tenant" and
        every tenant-scoped table reads as empty. A `refresh_from_db()` or an
        AuditLog count written outside this block does not fail; it silently finds
        nothing, and the assertion fails for the wrong reason.

        That is RLS working as designed (docs/02_Database.md, checklist item 3:
        fail closed). It just means assertions have to opt in.
        """
        return institution_db_context(self.institution.pk)

    def sign_in_owner(self):
        self.sign_in_with(self.OWNER_EMAIL)

    def sign_in_as(self, role, email=None):
        """Create an active account in this school with `role` and sign in as it."""
        email = email or f"{role.lower()}@sunrise.example"
        user = self.add_staff(role, email)
        self.client.logout()
        self.sign_in_with(email)
        return user

    def sign_in_with(self, email):
        """POST the real login form rather than calling client.login().

        client.login() invokes django.contrib.auth.login() with no request, so
        nothing has stamped SESSION_CONTEXT — and the last_login write that
        user_logged_in triggers then updates zero rows and raises
        Model.NotUpdated. That failure would come from the fixture, not the code
        under test. InstitutionLoginView stamps the institution around that write
        (accounts/views.py), so going through the door that exists in production
        is both correct here and a free check that the door still opens.
        """
        response = self.client.post(
            reverse("accounts:login"), {"username": email, "password": PASSWORD}
        )
        form = (response.context or {}).get("form") if response.status_code == 200 else None
        self.assertEqual(
            response.status_code,
            302,
            f"{email} could not sign in — Phase 2's exit condition. "
            f"Form errors: {form.errors.as_data() if form else 'none reported'}",
        )
        self.assertIn("_auth_user_id", self.client.session)

    def add_staff(self, role, email):
        """A second account in the same school, already active."""
        with institution_db_context(self.institution.pk):
            return User.objects.create_user(
                email=email,
                institution=self.institution,
                role=role,
                full_name=f"{role} Person",
                password=PASSWORD,
                is_active=True,
            )

    def settings_post(self, **overrides):
        """The institution settings form as a browser would return it.

        Pre-filled from the row rather than from constants, because several tests
        turn on `changed_data` being accurate. A dict of literals would differ
        from the registered institution in fields nobody edited, and a "saved with
        no changes" test would then be testing the opposite.
        """
        self.institution.refresh_from_db()
        data = {
            "name": self.institution.name,
            "code": self.institution.code,
            "type": self.institution.type,
            "timezone": self.institution.timezone,
            "email": self.institution.email,
            "phone": self.institution.phone,
            "address": self.institution.address,
        }
        data.update(overrides)
        return data

    def configure_school(self, class_names=("JSS 1A", "JSS 1B")):
        """Fast-forward past the wizard, for tests about what comes after it.

        Writes the same rows the wizard writes rather than driving the screens.
        The wizard itself is covered by core/tests/test_setup_and_dashboard.py;
        replaying three POSTs in every later test would make those tests fail for
        wizard reasons.

        Returns (session, term, classes).
        """
        with institution_db_context(self.institution.pk):
            session = Session.unscoped.create(
                institution=self.institution,
                name="2026/2027",
                start_date=SESSION_START,
                end_date=SESSION_END,
                is_current=True,
            )
            term = Term.unscoped.create(
                institution=self.institution,
                session=session,
                name="First Term",
                start_date=TERM_START,
                end_date=TERM_END,
                is_current=True,
            )
            classes = [
                Class.unscoped.create(
                    institution=self.institution, name=name, order=(index + 1) * 10
                )
                for index, name in enumerate(class_names)
            ]
        return session, term, classes

    def enroll_a_student(self, klass, session, term, admission_suffix="000001"):
        """One active student, enrolled in `klass`.

        Imported locally: `students` is a downstream app, and a module-level
        import here would put a Phase 4 model in the import path of every Phase 3
        test.
        """
        from students.models import Gender, Student, StudentEnrollment

        with institution_db_context(self.institution.pk):
            student = Student.unscoped.create(
                institution=self.institution,
                first_name="Chidi",
                last_name="Okeke",
                gender=Gender.MALE,
                date_of_birth=datetime.date(2012, 4, 2),
                admission_number=f"{self.institution.code}-2026-{admission_suffix}",
                guardian_name="Ngozi Okeke",
                guardian_phone="08010000000",
            )
            StudentEnrollment.unscoped.create(
                institution=self.institution,
                student=student,
                klass=klass,
                session=session,
                term=term,
            )
        return student
