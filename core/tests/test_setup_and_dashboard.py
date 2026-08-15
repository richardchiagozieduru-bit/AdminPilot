"""
Phase 2 and Phase 3 exit conditions, joined up.

docs/07_Implementation_Roadmap.md, Phase 2: "a registration can be submitted,
approved by a Super Admin, and the resulting Owner can log in and reach an empty
Dashboard."

docs/07_Implementation_Roadmap.md, Phase 3: "a freshly-approved Owner can complete
setup and reach a populated (empty-but-configured) Dashboard with at least one
Class defined."

Those are one continuous journey, and it is walked as one in
RegistrationToDashboardJourneyTests below. accounts/tests.py owns the registration
form and the login gate in isolation; platform_admin/tests.py owns approval and
the Super Admin isolation guarantee. What is only testable here is the seam: that
the state each of those leaves behind is the state the next screen expects.

Everything runs against SQL Server with RLS live. That is not incidental — the
wizard writes a Session, a Term and several Classes through a real request, so a
pass here is also evidence that TenantContextMiddleware stamps SESSION_CONTEXT
early enough for the view's own inserts to satisfy the BLOCK predicate.
"""

import datetime
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from academic.models import Class, ClassStatus, Session, Term
from accounts.models import User
from core.middleware import institution_db_context
from core.models import AuditLog, Institution
from core.tests.school import (
    PASSWORD,
    SESSION_END,
    SESSION_START,
    TERM_END,
    TERM_START,
    ApprovedSchoolTestCase,
)
from platform_admin.models import PlatformUser


def details_post(**overrides):
    """Wizard step 1, as the browser would send it.

    A ModelForm posts every field it renders, so a partial dict here would fail
    validation on the untouched ones and the failure would read as a wizard bug.

    Institution Settings tests use ApprovedSchoolTestCase.settings_post instead:
    that screen's tests turn on which fields actually changed, so they have to
    post the row's own values back.
    """
    data = {
        "name": "Sunrise Academy",
        "code": "SA",
        "type": Institution.Type.SECONDARY,
        "timezone": "Africa/Lagos",
        "email": "office@sunrise.example",
        "phone": "08030000000",
        "address": "12 School Road, Ikeja",
    }
    data.update(overrides)
    return data


def academic_post(**overrides):
    """Wizard step 2. `term-` prefixed keys are the second form on the screen.

    SessionForm and TermForm both have name/start_date/end_date, so the term form
    carries a prefix — see InstitutionSetupWizardView.build_term_form. Posting
    unprefixed term keys would silently overwrite the session's.
    """
    data = {
        "name": "2026/2027",
        "start_date": SESSION_START.isoformat(),
        "end_date": SESSION_END.isoformat(),
        "term-name": "First Term",
        "term-start_date": TERM_START.isoformat(),
        "term-end_date": TERM_END.isoformat(),
    }
    data.update(overrides)
    return data


class RegistrationToDashboardJourneyTests(TestCase):
    """One school, from the public registration form to a configured dashboard.

    Deliberately one long test. The exit conditions are journeys, and the failure
    this is guarding against is a seam — a step that works in isolation and leaves
    behind state the next step cannot use. Split into eight tests with shared
    fixtures, each step would be handed the state it wants instead of the state
    its predecessor actually produced, and the seam would stop being tested.

    Every step asserts before moving on, so a failure names the step.
    """

    def test_register_approve_sign_in_configure_and_land(self):
        # --- Phase 2, step 1: the public registration form ---------------------
        response = self.client.post(
            reverse("accounts:register"),
            {
                "school_name": "Sunrise Academy",
                "school_type": Institution.Type.SECONDARY,
                "owner_name": "Ada Obi",
                "owner_email": "ada@sunrise.example",
                "owner_phone": "08030000000",
                "password1": PASSWORD,
                "password2": PASSWORD,
            },
        )
        self.assertRedirects(response, reverse("accounts:register_pending"))

        institution = Institution.objects.get(name="Sunrise Academy")
        self.assertEqual(institution.status, Institution.Status.PENDING)

        # --- Phase 2, step 2: the Owner cannot get in yet ----------------------
        # Asserted against the login form, not authenticate(). The backend
        # deliberately returns inactive users so the form can name the real
        # reason (accounts/backends.py) — so "did authenticate() succeed" is the
        # wrong question. What matters is that no session is established.
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "ada@sunrise.example", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            "_auth_user_id",
            self.client.session,
            "A pending institution's Owner got a session — the gate is open.",
        )

        # --- Phase 2, step 3: a Super Admin approves, over HTTP ----------------
        PlatformUser.objects.create_user(
            email="reviewer@adminpilot.test",
            password=PASSWORD,
            full_name="Platform Reviewer",
        )
        self.assertTrue(
            self.client.login(
                username="reviewer@adminpilot.test", password=PASSWORD
            )
        )
        response = self.client.post(
            reverse("platform_admin:approve_institution", args=[institution.pk])
        )
        self.assertEqual(response.status_code, 302)
        institution.refresh_from_db()
        self.assertEqual(institution.status, Institution.Status.APPROVED)
        self.client.logout()

        # --- Phase 2, step 4: now the Owner can, and lands somewhere usable ----
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "ada@sunrise.example", "password": PASSWORD},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("_auth_user_id", self.client.session)

        # An Owner with no classes is sent to setup rather than to an empty
        # dashboard. Phase 2's "empty Dashboard" and Phase 3's wizard meet here:
        # the dashboard is reachable and renders, it just isn't the destination
        # while there is nothing on it.
        response = self.client.get(reverse("core:dashboard"))
        self.assertRedirects(response, reverse("core:setup"))

        # --- Phase 3, step 1: institution details ------------------------------
        response = self.client.get(reverse("core:setup"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["step"], "details")
        self.assertContains(response, "Welcome to AdminPilot")

        response = self.client.post(reverse("core:setup"), details_post(code="SUNRISE"))
        self.assertRedirects(response, reverse("core:setup"))
        institution.refresh_from_db()
        self.assertEqual(institution.code, "SUNRISE")
        self.assertEqual(institution.timezone, "Africa/Lagos")

        # --- Phase 3, step 2: first session and first term ---------------------
        response = self.client.get(reverse("core:setup"))
        self.assertEqual(response.context["step"], "academic")
        self.assertContains(response, "Your first session and term")

        response = self.client.post(reverse("core:setup"), academic_post())
        self.assertRedirects(response, reverse("core:setup"))

        with institution_db_context(institution.pk):
            session = Session.objects.get()
            term = Term.objects.get()
        self.assertEqual(session.name, "2026/2027")
        self.assertTrue(session.is_current)
        self.assertEqual(term.session_id, session.pk)
        self.assertTrue(
            term.is_current,
            "The wizard's term must be current, or the dashboard it hands over "
            "to still says 'No current term set'.",
        )

        # --- Phase 3, step 3: the initial class list ---------------------------
        response = self.client.get(reverse("core:setup"))
        self.assertEqual(response.context["step"], "classes")
        self.assertContains(response, "Your classes")

        response = self.client.post(
            reverse("core:setup"), {"names": "JSS 1A\nJSS 1B\n\nJSS 2A\n"}
        )
        self.assertRedirects(response, reverse("core:dashboard"))

        with institution_db_context(institution.pk):
            names = list(Class.objects.order_by("order").values_list("name", flat=True))
        self.assertEqual(
            names,
            ["JSS 1A", "JSS 1B", "JSS 2A"],
            "Classes must keep the order they were typed in, and the blank line "
            "must not become a class.",
        )

        # --- The exit condition: a populated, configured dashboard -------------
        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(
            response.status_code,
            200,
            "The dashboard still redirects after setup — the wizard and the "
            "dashboard are pointing at each other.",
        )
        self.assertEqual(response.context["class_count"], 3)
        self.assertEqual(response.context["student_count"], 0)
        self.assertEqual(response.context["current_term"], term)
        self.assertContains(response, "Active classes")
        self.assertNotContains(response, "No current term set")

        # And the classes are where the rest of the product will look for them.
        response = self.client.get(reverse("academic:class_list"))
        self.assertEqual(response.status_code, 200)
        for name in ("JSS 1A", "JSS 1B", "JSS 2A"):
            self.assertContains(response, name)


class WizardStepDerivationTests(ApprovedSchoolTestCase):
    """The wizard reads its step from the database, not from a counter.

    That is what makes it resumable and what makes a double-submit harmless, so
    it is worth testing directly rather than only through the happy path.
    """

    def setUp(self):
        self.sign_in_owner()

    def test_a_school_with_a_session_resumes_at_the_class_step(self):
        """Closing the tab after step 2 must not restart at step 1.

        Nothing in the session cookie is relied on here: a brand-new client, with
        a session that has never seen the details step, still lands on classes.
        """
        with institution_db_context(self.institution.pk):
            Session.unscoped.create(
                institution=self.institution,
                name="2026/2027",
                start_date=SESSION_START,
                end_date=SESSION_END,
                is_current=True,
            )

        self.client.logout()
        self.sign_in_owner()

        response = self.client.get(reverse("core:setup"))
        self.assertEqual(response.context["step"], "classes")

    def test_resubmitting_the_academic_step_cannot_create_a_second_session(self):
        """The guard is structural: once a session exists the view is not on that
        step any more, so there is no handler to re-run."""
        self.client.post(reverse("core:setup"), details_post())
        self.client.post(reverse("core:setup"), academic_post())

        self.client.post(reverse("core:setup"), academic_post())

        with institution_db_context(self.institution.pk):
            self.assertEqual(Session.objects.count(), 1)
            self.assertEqual(Term.objects.count(), 1)

    def test_an_invalid_term_does_not_leave_the_session_behind(self):
        """Both forms are validated before either is saved.

        A saved session with a rejected term would push the next GET to the class
        step, and the school would end up with a session and no term — the one
        state that blocks enrolment and fee structures alike.
        """
        self.client.post(reverse("core:setup"), details_post())

        response = self.client.post(
            reverse("core:setup"),
            academic_post(**{"term-end_date": "2026-09-01"}),  # ends before it starts
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["step"], "academic")

        with institution_db_context(self.institution.pk):
            self.assertEqual(Session.objects.count(), 0)
            self.assertEqual(Term.objects.count(), 0)

    def test_a_term_outside_its_session_is_rejected(self):
        """TermForm's own range check cannot run on this screen — its `session`
        field is removed and the session does not exist yet — so the wizard
        checks by hand. This is that check."""
        self.client.post(reverse("core:setup"), details_post())

        response = self.client.post(
            reverse("core:setup"),
            academic_post(**{"term-start_date": "2026-08-01"}),  # before the session
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "must fall inside the session")
        with institution_db_context(self.institution.pk):
            self.assertEqual(Session.objects.count(), 0)

    def test_a_configured_school_is_sent_away_from_the_wizard(self):
        self.configure_school()
        response = self.client.get(reverse("core:setup"))
        self.assertRedirects(response, reverse("core:dashboard"))

    def test_deactivating_every_class_does_not_reopen_the_wizard(self):
        """The dashboard's redirect tests for a Class *row*, not an active one.

        A school between terms can legitimately have every class inactive.
        Sending them back through first-run setup would be wrong, and — because
        the wizard's class step refuses names that already exist — a dead end.
        """
        self.configure_school()
        with institution_db_context(self.institution.pk):
            Class.unscoped.filter(institution_id=self.institution.pk).update(
                status=ClassStatus.INACTIVE
            )

        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["class_count"], 0)

    def test_the_wizard_has_no_sidebar(self):
        """A half-configured school has nothing to navigate to.

        base.html makes the sidebar a block for exactly this; if it comes back,
        the wizard becomes skippable by clicking past it.
        """
        response = self.client.get(reverse("core:setup"))
        self.assertNotContains(response, 'class="sidebar"')

    def test_an_administrator_cannot_run_setup(self):
        """institution_settings is Owner-only in docs/04_Permission_Matrix.md,
        and the wizard writes through it."""
        self.client.logout()
        self.add_staff(User.Role.ADMINISTRATOR, "admin@sunrise.example")
        self.sign_in_with("admin@sunrise.example")

        response = self.client.get(reverse("core:setup"))
        self.assertEqual(response.status_code, 403)


class DashboardTests(ApprovedSchoolTestCase):
    def test_owner_sees_counts_and_bursar_never_receives_them(self):
        """docs/08_UI_UX.md: a Bursar gets a reduced context, not a filtered
        template. The numbers must be absent from the response, not hidden in
        it."""
        self.configure_school()

        self.sign_in_owner()
        owner_response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(owner_response.context["class_count"], 2)
        self.client.logout()

        self.add_staff(User.Role.BURSAR, "bursar@sunrise.example")
        self.sign_in_with("bursar@sunrise.example")
        bursar_response = self.client.get(reverse("core:dashboard"))

        self.assertEqual(bursar_response.status_code, 200)
        self.assertIsNone(bursar_response.context.get("student_count"))
        self.assertIsNone(bursar_response.context.get("class_count"))
        self.assertNotContains(bursar_response, "Active classes")

    def test_a_bursar_is_not_redirected_into_setup(self):
        """The redirect is the Owner's, because only an Owner can act on it.

        A Bursar signing in at a school that has not finished setup would
        otherwise be bounced to a screen their role is forbidden from — a 403 as
        the landing page.
        """
        self.add_staff(User.Role.BURSAR, "bursar@sunrise.example")
        self.sign_in_with("bursar@sunrise.example")

        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_no_current_term_is_stated_rather_than_left_blank(self):
        self.sign_in_owner()
        with institution_db_context(self.institution.pk):
            Class.unscoped.create(institution=self.institution, name="JSS 1A", order=10)

        response = self.client.get(reverse("core:dashboard"))
        self.assertIsNone(response.context["current_term"])
        self.assertContains(response, "No current term")

    def test_financial_widgets_are_absent_until_billing_ships(self):
        """Not zeros. A zero would read as a school that has collected nothing."""
        self.configure_school()
        self.sign_in_owner()

        response = self.client.get(reverse("core:dashboard"))
        self.assertFalse(response.context["financials_available"])
        self.assertNotContains(response, "Collected this term")

    def test_greeting_uses_the_institution_timezone(self):
        self.configure_school()
        self.sign_in_owner()

        response = self.client.get(reverse("core:dashboard"))
        self.assertRegex(
            response.context["greeting"], r"^Good (morning|afternoon|evening), Ada$"
        )

    def test_an_unrecognised_timezone_does_not_break_the_page(self):
        """A stored value the runtime cannot resolve is a data problem, not a
        reason to 500 the landing page for everyone at that school."""
        self.configure_school()
        Institution.objects.filter(pk=self.institution.pk).update(
            timezone="Mars/Olympus_Mons"
        )
        self.sign_in_owner()

        response = self.client.get(reverse("core:dashboard"))
        self.assertEqual(response.status_code, 200)


class InstitutionSettingsTests(ApprovedSchoolTestCase):
    """`/settings/institution/`, including the code lock (docs/02_Database.md)."""

    def setUp(self):
        self.configure_school()
        self.sign_in_owner()
        self.url = reverse("core:institution_settings")
        self.original_code = self.institution.code

    def test_settings_edits_the_signed_in_school_with_no_pk_in_the_url(self):
        """There is no institution id in the URL by design — it comes from the
        session. A settings screen that took a pk would be a tenant-selection
        parameter."""
        self.assertNotIn(str(self.institution.pk), self.url)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["institution"].pk, self.institution.pk)

    def test_the_code_is_editable_before_the_first_receipt(self):
        response = self.client.post(self.url, self.settings_post(code="SUNRISE-1"))
        self.assertRedirects(response, self.url)

        self.institution.refresh_from_db()
        self.assertEqual(self.institution.code, "SUNRISE-1")

    def test_a_lowercase_code_is_normalised_rather_than_rejected(self):
        self.client.post(self.url, self.settings_post(code="sunrise"))
        self.institution.refresh_from_db()
        self.assertEqual(self.institution.code, "SUNRISE")

    def test_a_code_with_punctuation_is_an_inline_error(self):
        response = self.client.post(self.url, self.settings_post(code="SUN RISE!"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "letters, numbers and hyphens",
            " ".join(response.context["form"].errors["code"]),
        )
        self.institution.refresh_from_db()
        self.assertEqual(self.institution.code, self.original_code)

    def test_the_code_locks_once_a_receipt_exists(self):
        """The real rule, against a real receipt row.

        The whole chain is built — student, structure, assignment, payment,
        receipt — rather than patching code_is_locked(), because the thing worth
        proving is that the query finds a receipt on SQL Server under RLS. A
        stubbed True would prove only that the stub was applied.
        """
        self.issue_a_receipt()

        response = self.client.get(self.url)
        self.assertTrue(response.context["code_is_locked"])
        self.assertTrue(response.context["form"].fields["code"].disabled)
        self.assertContains(response, "receipts have already been issued")

        # A crafted POST does not get around it: a disabled field means Django
        # reuses the instance's value and ignores whatever was sent.
        self.client.post(self.url, self.settings_post(code="RENAMED"))
        self.institution.refresh_from_db()
        self.assertEqual(
            self.institution.code,
            self.original_code,
            "A locked institution code was changed by a direct POST.",
        )

    def test_the_rest_of_the_settings_stay_editable_after_the_lock(self):
        """Only the code is frozen. A school that moves premises still has to be
        able to correct its own address."""
        self.issue_a_receipt()

        self.client.post(self.url, self.settings_post(name="Sunrise Model Academy"))
        self.institution.refresh_from_db()
        self.assertEqual(self.institution.name, "Sunrise Model Academy")

    def test_a_change_writes_one_audit_entry_naming_the_fields(self):
        self.client.post(self.url, self.settings_post(phone="08099999999"))

        with self.in_school():
            entries = AuditLog.objects.filter(action="institution.updated")
            self.assertEqual(
                entries.count(), 1, "CLAUDE.md rule 5: one mutation, one entry."
            )
            entry = entries.get()
            self.assertEqual(entry.actor.email, self.OWNER_EMAIL)
        self.assertEqual(entry.detail["fields"], ["phone"])

    def test_a_no_op_save_writes_no_audit_entry(self):
        """An audit log padded with entries that record nothing is a log nobody
        reads."""
        with self.in_school():
            before = AuditLog.objects.count()
        self.client.post(self.url, self.settings_post())
        with self.in_school():
            after = AuditLog.objects.count()
        self.assertEqual(before, after)

    def test_an_administrator_cannot_reach_settings(self):
        self.client.logout()
        self.add_staff(User.Role.ADMINISTRATOR, "admin@sunrise.example")
        self.sign_in_with("admin@sunrise.example")

        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(
            self.client.post(self.url, {"name": "Hijacked"}).status_code, 403
        )
        self.institution.refresh_from_db()
        self.assertEqual(self.institution.name, "Sunrise Academy")

    def issue_a_receipt(self):
        """One real receipt, and the rows it cannot exist without.

        Imported locally: this is Phase 5's object graph, and a module-level
        billing import in a core test would make the Phase 3 suite fail to load
        on any change to it.
        """
        from billing.models import (
            FeeStructure,
            Payment,
            PaymentMethod,
            Receipt,
            StudentFeeAssignment,
        )
        from students.models import Gender, Student

        code = self.institution.code
        with institution_db_context(self.institution.pk):
            session = Session.unscoped.filter(
                institution_id=self.institution.pk
            ).first()
            term = Term.unscoped.filter(institution_id=self.institution.pk).first()
            klass = Class.unscoped.filter(institution_id=self.institution.pk).first()

            student = Student.unscoped.create(
                institution=self.institution,
                first_name="Chidi",
                last_name="Okeke",
                gender=Gender.MALE,
                date_of_birth=datetime.date(2012, 4, 2),
                admission_number=f"{code}-2026-000001",
                guardian_name="Ngozi Okeke",
                guardian_phone="08010000000",
            )
            structure = FeeStructure.unscoped.create(
                institution=self.institution,
                name=f"{klass.name} First Term",
                klass=klass,
                session=session,
                term=term,
                total_amount=Decimal("50000.00"),
            )
            assignment = StudentFeeAssignment.unscoped.create(
                institution=self.institution,
                student=student,
                fee_structure=structure,
                amount_due=Decimal("50000.00"),
            )
            payment = Payment.unscoped.create(
                institution=self.institution,
                assignment=assignment,
                amount=Decimal("50000.00"),
                payment_date=TERM_START,
                method=PaymentMethod.CASH,
            )
            return Receipt.unscoped.create(
                institution=self.institution,
                payment=payment,
                receipt_number=f"{code}-2026-000001",
            )


class AuditLogViewTests(ApprovedSchoolTestCase):
    """Tiered visibility (docs/04_Permission_Matrix.md): Owner sees the school's
    activity, everyone else sees only their own."""

    def setUp(self):
        self.configure_school()

    def test_owner_sees_another_users_entries(self):
        admin = self.add_staff(User.Role.ADMINISTRATOR, "admin@sunrise.example")
        self.sign_in_with("admin@sunrise.example")
        self.client.post(
            reverse("academic:class_add"), {"name": "JSS 3A", "order": 30, "status": ClassStatus.ACTIVE}
        )
        self.client.logout()

        self.sign_in_owner()
        response = self.client.get(reverse("core:audit_log"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["scope_is_own_only"])
        self.assertIn(
            admin.pk, [entry.actor_id for entry in response.context["entries"]]
        )

    def test_an_administrator_sees_only_their_own_entries(self):
        """A queryset filter, not a template condition — another user's summary
        text must not be in the response at all."""
        self.sign_in_owner()
        self.client.post(
            reverse("academic:class_add"), {"name": "OwnerClass", "order": 40, "status": ClassStatus.ACTIVE}
        )
        self.client.logout()

        self.add_staff(User.Role.ADMINISTRATOR, "admin@sunrise.example")
        self.sign_in_with("admin@sunrise.example")
        self.client.post(
            reverse("academic:class_add"), {"name": "AdminClass", "order": 50, "status": ClassStatus.ACTIVE}
        )

        response = self.client.get(reverse("core:audit_log"))
        self.assertTrue(response.context["scope_is_own_only"])
        self.assertContains(response, "AdminClass")
        self.assertNotContains(response, "OwnerClass")
