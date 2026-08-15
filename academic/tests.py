"""
Class Management and Academic Structure — Phase 3's two ongoing screens.

Three rules here are load-bearing and each has a test that fails if it is
softened:

  * Deactivating a class with students still enrolled is *allowed*
    (docs/03_Views_and_Endpoints.md). It blocks new enrollments; it does not
    touch existing ones. A future "are you sure, it has students" refusal would
    be a scope change, and test_deactivation_is_allowed_with_students_enrolled
    is where it would be caught.
  * Class names are unique per institution, not globally. Two schools both
    having a "JSS 1A" is the normal case.
  * A Bursar reads this screen and cannot write to it. The refusal happens
    *before* the view body — RoleRequiredMixin.check_access — so an unauthorised
    POST must leave no row behind, not create one and then 403.

`self.in_school()` wraps every assertion that reads or writes a tenant-scoped
table. A test method is not a request, and TenantContextMiddleware clears
SESSION_CONTEXT when the request it stamped ends — so an unwrapped
`refresh_from_db()` finds nothing and fails for the wrong reason. See
core/tests/school.py.

The permission checks run against real requests rather than by calling
has_module_access directly: the matrix is already unit-tested, and what could
still break is a view forgetting to consult it.
"""

from django.test import TestCase
from django.urls import reverse

from accounts.models import User
from core.middleware import institution_db_context
from core.models import AuditLog, Institution
from core.tests.school import (
    SESSION_END,
    SESSION_START,
    TERM_END,
    TERM_START,
    ApprovedSchoolTestCase,
)

from .forms import ClassForm, SessionForm, SetupClassesForm, TermForm
from .models import Class, ClassStatus, Session, Term
from .services import create_classes, set_current_session, set_current_term

NEXT_SESSION_END = SESSION_END.replace(year=SESSION_END.year + 1)


def other_school():
    """A second tenant. Institution itself carries no institution_id, so it is
    not behind the RLS predicate and needs no context to create."""
    return Institution.objects.create(
        name="Beta School", code="BETA", status=Institution.Status.APPROVED
    )


class ClassListTests(ApprovedSchoolTestCase):
    def setUp(self):
        self.session, self.term, self.classes = self.configure_school()
        self.url = reverse("academic:class_list")

    def test_the_list_shows_every_class_with_its_enrolled_count(self):
        self.enroll_a_student(self.classes[0], self.session, self.term)
        self.sign_in_owner()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        with self.in_school():
            counts = {
                klass.name: klass.student_count
                for klass in response.context["classes"]
            }
        self.assertEqual(counts, {"JSS 1A": 1, "JSS 1B": 0})

    def test_counts_are_for_the_current_term_only(self):
        """A class's count is "who is in it now", not "who ever was".

        Without the term filter on the annotation, a school in its third term
        would show three years of enrolments stacked in every class.
        """
        self.enroll_a_student(self.classes[0], self.session, self.term)

        with self.in_school():
            next_term = Term.unscoped.create(
                institution=self.institution,
                session=self.session,
                name="Second Term",
                start_date=TERM_END,
                end_date=SESSION_END,
            )
            set_current_term(self.institution.pk, next_term)

        self.sign_in_owner()
        response = self.client.get(self.url)
        with self.in_school():
            counts = {
                klass.name: klass.student_count
                for klass in response.context["classes"]
            }
        self.assertEqual(counts, {"JSS 1A": 0, "JSS 1B": 0})

    def test_no_current_term_means_no_counts_rather_than_wrong_counts(self):
        self.enroll_a_student(self.classes[0], self.session, self.term)
        with self.in_school():
            Term.unscoped.filter(institution_id=self.institution.pk).update(
                is_current=False
            )

        self.sign_in_owner()
        response = self.client.get(self.url)
        with self.in_school():
            first = response.context["classes"][0]
        self.assertIsNone(
            getattr(first, "student_count", None),
            "With no current term the queryset must not be annotated at all — an "
            "unfiltered count would show every enrolment the class ever had.",
        )
        self.assertContains(response, "no current term set")

    def test_another_schools_classes_are_not_listed(self):
        """The tenant filter, through a real request.

        Belt and braces over RLS: this is the application layer agreeing with the
        database, and it is the layer a mistake is most likely to be made in.
        """
        other = other_school()
        with institution_db_context(other.pk):
            Class.unscoped.create(institution=other, name="Beta Only", order=10)

        self.sign_in_owner()
        response = self.client.get(self.url)
        self.assertNotContains(response, "Beta Only")
        self.assertEqual(len(response.context["classes"]), 2)


class ClassCrudTests(ApprovedSchoolTestCase):
    def setUp(self):
        self.session, self.term, self.classes = self.configure_school()
        self.sign_in_owner()

    def test_adding_a_class_writes_the_row_and_an_audit_entry(self):
        response = self.client.post(
            reverse("academic:class_add"),
            {"name": "JSS 2A", "order": 30, "status": ClassStatus.ACTIVE},
        )
        self.assertRedirects(response, reverse("academic:class_list"))

        with self.in_school():
            klass = Class.objects.get(name="JSS 2A")
            self.assertEqual(klass.institution_id, self.institution.pk)
            entry = AuditLog.objects.filter(action="class.created").get()
        self.assertEqual(entry.target_id, str(klass.pk))
        self.assertIn("JSS 2A", entry.summary)

    def test_a_duplicate_name_is_a_field_error_not_a_database_error(self):
        """CLAUDE.md: never hand a raw database error back.

        uniq_class_name_per_inst would raise IntegrityError; ClassForm asks first
        so the answer is an inline message on the field.
        """
        response = self.client.post(
            reverse("academic:class_add"),
            {"name": "jss 1a", "order": 30, "status": ClassStatus.ACTIVE},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "already have a class with this name",
            " ".join(response.context["form"].errors["name"]),
        )
        with self.in_school():
            self.assertEqual(Class.objects.count(), 2)

    def test_the_same_name_in_another_school_is_fine(self):
        """Uniqueness is per institution. Two schools with a "JSS 1A" is normal.

        Validated inside the other school's own context, so the duplicate check
        really runs there. Outside any context RLS returns zero rows and the form
        would validate for the wrong reason.
        """
        other = other_school()
        with institution_db_context(other.pk):
            form = ClassForm(
                data={"name": "JSS 1A", "order": 10, "status": ClassStatus.ACTIVE},
                institution_id=other.pk,
            )
            self.assertTrue(form.is_valid(), form.errors)

    def test_renaming_a_class_logs_the_changed_fields(self):
        klass = self.classes[0]
        response = self.client.post(
            reverse("academic:class_edit", args=[klass.pk]),
            {"name": "JSS 1 Alpha", "order": klass.order, "status": klass.status},
        )
        self.assertRedirects(response, reverse("academic:class_list"))

        with self.in_school():
            klass.refresh_from_db()
            entry = AuditLog.objects.filter(action="class.updated").get()
        self.assertEqual(klass.name, "JSS 1 Alpha")
        self.assertEqual(entry.detail["fields"], ["name"])

    def test_editing_another_schools_class_is_a_404(self):
        """Not a 403. A 403 would confirm the id exists."""
        other = other_school()
        with institution_db_context(other.pk):
            foreign = Class.unscoped.create(
                institution=other, name="Beta Only", order=10
            )

        response = self.client.post(
            reverse("academic:class_edit", args=[foreign.pk]),
            {"name": "Hijacked", "order": 10, "status": ClassStatus.ACTIVE},
        )
        self.assertEqual(response.status_code, 404)

        with institution_db_context(other.pk):
            foreign.refresh_from_db()
        self.assertEqual(foreign.name, "Beta Only")


class ClassDeactivationTests(ApprovedSchoolTestCase):
    """The rule most likely to be "helpfully" tightened later."""

    def setUp(self):
        self.session, self.term, self.classes = self.configure_school()
        self.klass = self.classes[0]
        self.sign_in_owner()

    def test_deactivation_is_allowed_with_students_enrolled(self):
        """docs/03_Views_and_Endpoints.md: deactivation blocks new enrollments and
        leaves existing ones alone. Enrolled students are not a reason to refuse.
        """
        from students.models import StudentStatus

        student = self.enroll_a_student(self.klass, self.session, self.term)

        response = self.client.post(
            reverse("academic:class_deactivate", args=[self.klass.pk])
        )
        self.assertRedirects(response, reverse("academic:class_list"))

        with self.in_school():
            self.klass.refresh_from_db()
            # The student is untouched: same class, same status, still enrolled.
            student.refresh_from_db()
            enrollments = list(student.enrollments.all())

        self.assertEqual(self.klass.status, ClassStatus.INACTIVE)
        self.assertEqual(len(enrollments), 1)
        self.assertEqual(enrollments[0].klass_id, self.klass.pk)
        self.assertEqual(student.status, StudentStatus.ACTIVE)

    def test_deactivation_is_post_only(self):
        """A state change reachable by GET can be fired by a link prefetch."""
        response = self.client.get(
            reverse("academic:class_deactivate", args=[self.klass.pk])
        )
        self.assertEqual(response.status_code, 405)
        with self.in_school():
            self.klass.refresh_from_db()
        self.assertEqual(self.klass.status, ClassStatus.ACTIVE)

    def test_deactivation_writes_an_audit_entry_naming_the_operation(self):
        self.client.post(reverse("academic:class_deactivate", args=[self.klass.pk]))

        with self.in_school():
            entry = AuditLog.objects.filter(action="class.deactivated").get()
            actor_email = entry.actor.email
        self.assertIn(self.klass.name, entry.summary)
        self.assertEqual(actor_email, self.OWNER_EMAIL)

    def test_deactivating_twice_is_a_no_op_rather_than_a_second_log_entry(self):
        url = reverse("academic:class_deactivate", args=[self.klass.pk])
        self.client.post(url)
        self.client.post(url)

        with self.in_school():
            logged = AuditLog.objects.filter(action="class.deactivated").count()
        self.assertEqual(
            logged,
            1,
            "A repeated POST logged a second deactivation of an already-inactive "
            "class.",
        )

    def test_reactivation_restores_the_class(self):
        self.client.post(reverse("academic:class_deactivate", args=[self.klass.pk]))
        response = self.client.post(
            reverse("academic:class_reactivate", args=[self.klass.pk])
        )
        self.assertRedirects(response, reverse("academic:class_list"))

        with self.in_school():
            self.klass.refresh_from_db()
            reactivated = AuditLog.objects.filter(action="class.reactivated").exists()
        self.assertEqual(self.klass.status, ClassStatus.ACTIVE)
        self.assertTrue(reactivated)

    def test_deactivating_another_schools_class_is_a_404(self):
        other = other_school()
        with institution_db_context(other.pk):
            foreign = Class.unscoped.create(
                institution=other, name="Beta Only", order=10
            )

        response = self.client.post(
            reverse("academic:class_deactivate", args=[foreign.pk])
        )
        self.assertEqual(response.status_code, 404)
        with institution_db_context(other.pk):
            foreign.refresh_from_db()
        self.assertEqual(foreign.status, ClassStatus.ACTIVE)


class ClassRoleGatingTests(ApprovedSchoolTestCase):
    """docs/04_Permission_Matrix.md: classes — view for all, manage for
    Owner/Administrator."""

    def setUp(self):
        self.session, self.term, self.classes = self.configure_school()
        self.klass = self.classes[0]

    def test_a_bursar_can_read_the_list_without_edit_controls(self):
        """The controls are absent from the markup, not disabled in it. A Bursar's
        page contains no path they would be refused on."""
        self.sign_in_as(User.Role.BURSAR)

        response = self.client.get(reverse("academic:class_list"))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["can_manage"])
        self.assertContains(response, "JSS 1A")
        self.assertNotContains(response, reverse("academic:class_add"))
        self.assertNotContains(
            response, reverse("academic:class_deactivate", args=[self.klass.pk])
        )

    def test_a_bursar_post_creates_nothing(self):
        """The ordering that RoleRequiredMixin.check_access exists for.

        A permission check that ran after the view body would let this POST insert
        the row and *then* return 403 — an honest refusal with the row already
        written. The count is the assertion that matters here; the status code
        alone would pass either way.
        """
        self.sign_in_as(User.Role.BURSAR)

        response = self.client.post(
            reverse("academic:class_add"),
            {"name": "Bursar Class", "order": 99, "status": ClassStatus.ACTIVE},
        )

        self.assertEqual(response.status_code, 403)
        with self.in_school():
            self.assertEqual(
                Class.objects.count(),
                2,
                "A Bursar's POST created a class before being refused.",
            )

    def test_a_bursar_cannot_deactivate(self):
        self.sign_in_as(User.Role.BURSAR)

        response = self.client.post(
            reverse("academic:class_deactivate", args=[self.klass.pk])
        )
        self.assertEqual(response.status_code, 403)
        with self.in_school():
            self.klass.refresh_from_db()
        self.assertEqual(self.klass.status, ClassStatus.ACTIVE)

    def test_an_administrator_can_manage_classes(self):
        self.sign_in_as(User.Role.ADMINISTRATOR)

        response = self.client.post(
            reverse("academic:class_add"),
            {"name": "JSS 3A", "order": 30, "status": ClassStatus.ACTIVE},
        )
        self.assertRedirects(response, reverse("academic:class_list"))

    def test_a_staff_account_reaches_nothing(self):
        """Staff is inert in V1 — in the enum so a future module needs no
        migration, in no permission set."""
        self.sign_in_as(User.Role.STAFF)

        self.assertEqual(
            self.client.get(reverse("academic:class_list")).status_code, 403
        )


class AcademicStructureTests(ApprovedSchoolTestCase):
    """`/settings/academic/` — ongoing Session/Term creation. Owner only."""

    def setUp(self):
        self.session, self.term, self.classes = self.configure_school()
        self.sign_in_owner()
        self.url = reverse("academic:structure")

    def test_the_screen_names_the_current_term(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["current_term"], self.term)
        self.assertContains(response, "First Term")

    def test_adding_a_session_does_not_make_it_current(self):
        """A school entering next year's session in March is still teaching this
        year's. Moving the dashboard's term as a side effect of adding a row would
        be wrong."""
        response = self.client.post(
            self.url,
            {
                "action": "add_session",
                "name": "2027/2028",
                "start_date": SESSION_END.isoformat(),
                "end_date": NEXT_SESSION_END.isoformat(),
            },
        )
        self.assertRedirects(response, self.url)

        with self.in_school():
            added = Session.objects.get(name="2027/2028")
            self.session.refresh_from_db()
        self.assertFalse(added.is_current)
        self.assertTrue(self.session.is_current)

    def test_adding_a_term_writes_the_row_and_an_audit_entry(self):
        """The happy path, which nothing covered until a bug lived in it.

        Every other add_term test asserted a *refusal*, and a form whose session
        dropdown rejects every value refuses in exactly the same way as a form
        working correctly. So the screen was unable to add a term at all while the
        suite stayed green — see core/tests/test_forms.py.
        """
        response = self.client.post(
            self.url,
            {
                "action": "add_term",
                "session": self.session.pk,
                "name": "Second Term",
                "start_date": "2027-01-11",
                "end_date": "2027-04-02",
            },
        )
        self.assertRedirects(response, self.url)

        with self.in_school():
            added = Term.objects.get(name="Second Term")
            self.assertEqual(added.session_id, self.session.pk)
            self.assertEqual(added.institution_id, self.institution.pk)
            self.assertFalse(
                added.is_current,
                "Adding a term must not move the school's current term.",
            )
            entry = AuditLog.objects.filter(action="term.created").get()
        self.assertEqual(entry.target_id, str(added.pk))

    def test_adding_a_term_outside_its_session_is_refused(self):
        """Dates that look like a school term but fall past the session's own end
        date — the shape of "picked the wrong session in the dropdown". Both dates
        land outside the window, so both are flagged; TermForm checks against the
        session it was given, not the calendar.
        """
        response = self.client.post(
            self.url,
            {
                "action": "add_term",
                "session": self.session.pk,
                "name": "Second Term",
                "start_date": "2027-09-06",
                "end_date": "2027-12-17",
            },
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["term_form"]
        self.assertIn("start_date", form.errors)
        self.assertNotIn(
            "session",
            form.errors,
            "The session the school picked is one of its own — rejecting it means "
            "the dropdown is being validated against the wrong queryset.",
        )
        # The re-rendered field carries its accessibility markup. Asserted on the
        # HTML rather than on widget.attrs because the wiring happens during
        # validation and the template renders {{ field }} before it reads
        # field.errors — this is the assertion that catches the two drifting apart.
        self.assertContains(response, 'aria-invalid="true"')
        self.assertContains(response, 'id="id_start_date_errors"')
        with self.in_school():
            self.assertEqual(Term.objects.count(), 1)

    def test_setting_a_current_term_unsets_the_previous_one(self):
        """Exactly one current term per institution.

        Two rows flagged current is the failure that would make every downstream
        "the current term" lookup non-deterministic.
        """
        with self.in_school():
            second = Term.unscoped.create(
                institution=self.institution,
                session=self.session,
                name="Second Term",
                start_date=TERM_END,
                end_date=SESSION_END,
            )

        response = self.client.post(
            self.url, {"action": "set_current_term", "term_id": second.pk}
        )
        self.assertRedirects(response, self.url)

        with self.in_school():
            current = [term.pk for term in Term.objects.filter(is_current=True)]
        self.assertEqual(current, [second.pk])

    def test_setting_a_current_term_promotes_its_session(self):
        """A current term inside a non-current session is an inconsistent state:
        the dashboard would name one year and the term would belong to another."""
        with self.in_school():
            next_session = Session.unscoped.create(
                institution=self.institution,
                name="2027/2028",
                start_date=SESSION_END,
                end_date=NEXT_SESSION_END,
            )
            next_term = Term.unscoped.create(
                institution=self.institution,
                session=next_session,
                name="First Term",
                start_date=SESSION_END,
                end_date=NEXT_SESSION_END,
            )

        self.client.post(
            self.url, {"action": "set_current_term", "term_id": next_term.pk}
        )

        with self.in_school():
            next_session.refresh_from_db()
            self.session.refresh_from_db()
        self.assertTrue(next_session.is_current)
        self.assertFalse(self.session.is_current)

    def test_setting_a_current_session_writes_an_audit_entry(self):
        with self.in_school():
            other = Session.unscoped.create(
                institution=self.institution,
                name="2027/2028",
                start_date=SESSION_END,
                end_date=NEXT_SESSION_END,
            )

        self.client.post(
            self.url, {"action": "set_current_session", "session_id": other.pk}
        )

        with self.in_school():
            logged = AuditLog.objects.filter(action="session.set_current").exists()
        self.assertTrue(
            logged,
            "Changing the current session moves every downstream default — it is "
            "a sensitive mutation and CLAUDE.md rule 5 applies.",
        )

    def test_another_schools_term_cannot_be_made_current(self):
        other = other_school()
        with institution_db_context(other.pk):
            session = Session.unscoped.create(
                institution=other,
                name="2026/2027",
                start_date=SESSION_START,
                end_date=SESSION_END,
            )
            foreign_term = Term.unscoped.create(
                institution=other,
                session=session,
                name="First Term",
                start_date=TERM_START,
                end_date=TERM_END,
            )

        response = self.client.post(
            self.url, {"action": "set_current_term", "term_id": foreign_term.pk}
        )
        self.assertEqual(response.status_code, 404)

        with institution_db_context(other.pk):
            foreign_term.refresh_from_db()
        self.assertFalse(foreign_term.is_current)

    def test_an_unknown_action_changes_nothing(self):
        response = self.client.post(self.url, {"action": "drop_everything"})
        self.assertRedirects(response, self.url)
        with self.in_school():
            self.assertEqual(Session.objects.count(), 1)

    def test_an_administrator_cannot_reach_the_structure_screen(self):
        """academic_structure is Owner-only in docs/04_Permission_Matrix.md."""
        self.sign_in_as(User.Role.ADMINISTRATOR)
        self.assertEqual(self.client.get(self.url).status_code, 403)


class FormScopingTests(ApprovedSchoolTestCase):
    """The forms take `institution_id` explicitly. These are the checks that
    would silently pass against the wrong tenant if that argument were optional.
    """

    def setUp(self):
        self.session, self.term, self.classes = self.configure_school()

    def test_the_term_session_dropdown_offers_this_schools_sessions(self):
        with self.in_school():
            form = TermForm(institution_id=self.institution.pk)
            self.assertIn(self.session, list(form.fields["session"].queryset))

    def test_a_term_inside_this_schools_own_session_validates(self):
        """The control for the test below it.

        A dropdown that rejects everything refuses a foreign session id for the
        right reason by accident. This is the assertion that tells the two apart,
        and it is the one that was failing.
        """
        with self.in_school():
            form = TermForm(
                data={
                    "session": self.session.pk,
                    "name": "Second Term",
                    "start_date": "2027-01-11",
                    "end_date": "2027-04-02",
                },
                institution_id=self.institution.pk,
            )
            self.assertTrue(form.is_valid(), form.errors.as_data())

    def test_a_term_cannot_be_attached_to_another_schools_session(self):
        """The dropdown's contents are one thing; what a hand-built POST can do is
        the one that matters. A foreign session id has to come back as a field
        error, not a term filed under someone else's year."""
        other = other_school()
        with institution_db_context(other.pk):
            foreign_session = Session.unscoped.create(
                institution=other,
                name="2026/2027",
                start_date=SESSION_START,
                end_date=SESSION_END,
            )

        with self.in_school():
            form = TermForm(
                data={
                    "session": foreign_session.pk,
                    "name": "First Term",
                    "start_date": TERM_START.isoformat(),
                    "end_date": TERM_END.isoformat(),
                },
                institution_id=self.institution.pk,
            )
            self.assertFalse(form.is_valid())
            self.assertIn("session", form.errors)

    def test_institution_id_is_required_rather_than_defaulted(self):
        """A uniqueness check that silently ran against every institution, or
        against none, would be worse than a TypeError."""
        for form_class in (SessionForm, TermForm, ClassForm, SetupClassesForm):
            with self.subTest(form=form_class.__name__):
                with self.assertRaises(TypeError):
                    form_class(data={})

    def test_a_session_name_must_be_unique_within_the_school_only(self):
        payload = {
            "name": "2026/2027",
            "start_date": SESSION_START.isoformat(),
            "end_date": SESSION_END.isoformat(),
        }

        with self.in_school():
            clashing = SessionForm(data=payload, institution_id=self.institution.pk)
            self.assertIn("name", clashing.errors)

        other = other_school()
        with institution_db_context(other.pk):
            fine = SessionForm(data=payload, institution_id=other.pk)
            self.assertTrue(fine.is_valid(), fine.errors)

    def test_the_setup_class_list_rejects_duplicates_within_one_submission(self):
        with self.in_school():
            form = SetupClassesForm(
                data={"names": "JSS 2A\nJSS 2B\njss 2a"},
                institution_id=self.institution.pk,
            )
            self.assertIn("names", form.errors)
            self.assertIn("listed more than once", " ".join(form.errors["names"]))

    def test_the_setup_class_list_rejects_names_that_already_exist(self):
        with self.in_school():
            form = SetupClassesForm(
                data={"names": "JSS 1A"}, institution_id=self.institution.pk
            )
            self.assertIn("names", form.errors)

    def test_blank_lines_are_dropped_and_order_is_preserved(self):
        with self.in_school():
            form = SetupClassesForm(
                data={"names": "  JSS 2A  \n\n\nJSS 2B\n"},
                institution_id=self.institution.pk,
            )
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["names"], ["JSS 2A", "JSS 2B"])


class CreateClassesServiceTests(ApprovedSchoolTestCase):
    """create_classes is the wizard's write path; the wizard test covers it
    through HTTP. These cover the parts a single happy-path POST cannot show."""

    def test_order_continues_from_the_existing_highest(self):
        """Classes added later must sort after the ones already there, not
        collide at the front of the list."""
        _, _, classes = self.configure_school()
        highest = max(klass.order for klass in classes)

        with self.in_school():
            created = create_classes(self.institution.pk, ["JSS 2A", "JSS 2B"])

        self.assertEqual(
            [klass.order for klass in created], [highest + 10, highest + 20]
        )

    def test_one_audit_entry_covers_the_batch(self):
        """Ten entries for one action taken on one screen would bury the log."""
        with self.in_school():
            create_classes(self.institution.pk, ["JSS 1A", "JSS 1B", "JSS 2A"])
            entries = AuditLog.objects.filter(action="class.created")
            self.assertEqual(entries.count(), 1)
            entry = entries.get()
        self.assertEqual(entry.detail["names"], ["JSS 1A", "JSS 1B", "JSS 2A"])
        self.assertIn("3 classes", entry.summary)

    def test_set_current_session_leaves_exactly_one_current(self):
        """Called directly because the ordering inside it matters: unset first,
        then set. The reverse would briefly leave two rows current."""
        self.configure_school()

        with self.in_school():
            other = Session.unscoped.create(
                institution=self.institution,
                name="2027/2028",
                start_date=SESSION_END,
                end_date=NEXT_SESSION_END,
            )
            set_current_session(self.institution.pk, other)
            current = [s.pk for s in Session.objects.filter(is_current=True)]
        self.assertEqual(current, [other.pk])


class AnonymousAccessTests(TestCase):
    """No screen in this app is public."""

    def test_every_academic_url_redirects_an_anonymous_caller_to_login(self):
        for name, args in (
            ("academic:class_list", ()),
            ("academic:class_add", ()),
            ("academic:structure", ()),
            ("academic:class_edit", (1,)),
        ):
            with self.subTest(url=name):
                response = self.client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("accounts:login"), response["Location"])

    def test_the_login_redirect_does_not_hit_the_database_for_a_class(self):
        """The access check runs before get_object.

        An anonymous request for a class edit URL must be an unauthenticated
        redirect, not a 404 — a 404 here would mean the view looked the row up
        first, and would tell a stranger which ids exist.
        """
        response = self.client.get(reverse("academic:class_edit", args=[999999]))
        self.assertEqual(response.status_code, 302)
