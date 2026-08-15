"""
Phase 2 exit conditions, Super Admin half — plus the standing guarantee from
CLAUDE.md rule 3: "Super Admin (platform staff) never reads institution data."

The roadmap asks for that path to be confirmed as having "zero queries touching
any tenant-scoped table". That is asserted here by capturing the SQL of every
query a request makes and checking it against the list of tenant-scoped table
names, rather than by reading the code and being satisfied — a future edit that
adds a student count to the review screen would fail this file.

The one deliberate exception is documented on
test_approval_touches_users_and_nothing_else: approval activates the Owner, which
is a write into `users`. That is the whole mechanism by which approval grants
access, and it is one UPDATE against one column.
"""

from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.db import connection
from django.urls import reverse

from accounts.models import User
from accounts.services import register_institution
from core.middleware import institution_db_context
from core.models import Institution
from platform_admin.models import PlatformUser
from platform_admin.services import ApprovalError, approve_institution, reject_institution

# Every table carrying an institution_id (docs/02_Database.md). `users` is listed
# separately below because approval writes to it by design.
TENANT_TABLES = (
    "students",
    "student_enrollments",
    "classes",
    "sessions",
    "terms",
    "fee_structures",
    "fee_structure_items",
    "student_fee_assignments",
    "payments",
    "receipts",
    "credit_transactions",
    "bulk_import_batches",
    "bulk_import_rows",
    "institution_number_sequences",
    "audit_logs",
)


def tenant_tables_in(queries):
    """Which tenant-scoped tables the captured SQL mentions."""
    hit = set()
    for query in queries:
        sql = query["sql"].lower()
        for table in TENANT_TABLES:
            if f"[{table}]" in sql or f" {table} " in sql:
                hit.add(table)
    return hit


class PlatformIsolationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.reviewer = PlatformUser.objects.create_user(
            email="reviewer@adminpilot.test",
            password="correct-horse-9",
            full_name="Platform Reviewer",
        )
        cls.pending = register_institution(
            school_name="Pending Academy",
            school_type=Institution.Type.SECONDARY,
            owner_name="Ada Obi",
            owner_email="ada@pending.example",
            owner_phone="",
            password="correct-horse-9",
        )

    def sign_in_reviewer(self):
        self.assertTrue(
            self.client.login(
                username="reviewer@adminpilot.test", password="correct-horse-9"
            )
        )


class PendingListTests(PlatformIsolationTestCase):
    def test_list_requires_a_platform_account(self):
        response = self.client.get(reverse("platform_admin:institution_list"))
        self.assertEqual(response.status_code, 302)

    def test_institution_user_cannot_reach_the_platform_list(self):
        """An approved Owner with a valid session is still not platform staff.

        SuperAdminRequiredMixin tests `isinstance(user, PlatformUser)`, not a role
        value — Super Admin is a separate model, so that is the only honest check.
        """
        approve_institution(self.pending, self.reviewer)
        self.client.post(
            reverse("accounts:login"),
            {"username": "ada@pending.example", "password": "correct-horse-9"},
        )
        response = self.client.get(reverse("platform_admin:institution_list"))
        self.assertIn(response.status_code, (302, 403))

    def test_review_screen_queries_no_tenant_table(self):
        self.sign_in_reviewer()
        with CaptureQueriesContext(connection) as captured:
            response = self.client.get(reverse("platform_admin:institution_list"))
        self.assertEqual(response.status_code, 200)

        touched = tenant_tables_in(captured.captured_queries)
        self.assertEqual(
            touched,
            set(),
            f"The Super Admin review screen queried tenant-scoped tables: {touched}. "
            f"CLAUDE.md rule 3 makes this a structural guarantee, not a preference.",
        )

    def test_review_screen_shows_the_pending_school(self):
        self.sign_in_reviewer()
        response = self.client.get(reverse("platform_admin:institution_list"))
        self.assertContains(response, "Pending Academy")
        self.assertContains(response, "ada@pending.example")


class ApprovalTests(PlatformIsolationTestCase):
    def test_approval_activates_the_owner(self):
        approve_institution(self.pending, self.reviewer)

        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, Institution.Status.APPROVED)
        self.assertEqual(self.pending.reviewed_by, self.reviewer)
        self.assertIsNotNone(self.pending.reviewed_at)

        with institution_db_context(self.pending.pk):
            owner = User.objects.get(institution_id=self.pending.pk)
        self.assertTrue(owner.is_active)

    def test_approval_touches_users_and_nothing_else(self):
        """The one write into a tenant table on this path, and only that one.

        Activating the Owner is how approval grants access; there is no other
        mechanism. Every *other* tenant table must stay untouched.
        """
        with CaptureQueriesContext(connection) as captured:
            approve_institution(self.pending, self.reviewer)

        touched = tenant_tables_in(captured.captured_queries)
        self.assertEqual(
            touched,
            set(),
            f"Approval touched tenant-scoped tables beyond `users`: {touched}.",
        )
        self.assertTrue(
            any("users" in q["sql"].lower() for q in captured.captured_queries),
            "Approval must activate the Owner — if it stopped writing to `users`, "
            "an approved school could not log in.",
        )

    def test_approving_twice_is_refused(self):
        approve_institution(self.pending, self.reviewer)
        with self.assertRaises(ApprovalError):
            approve_institution(self.pending, self.reviewer)

    def test_approve_endpoint_is_post_only(self):
        self.sign_in_reviewer()
        response = self.client.get(
            reverse("platform_admin:approve_institution", args=[self.pending.pk])
        )
        self.assertEqual(response.status_code, 405)

    def test_approve_endpoint_approves(self):
        self.sign_in_reviewer()
        response = self.client.post(
            reverse("platform_admin:approve_institution", args=[self.pending.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, Institution.Status.APPROVED)


class RejectionTests(PlatformIsolationTestCase):
    def test_rejection_records_the_reason_and_keeps_the_row(self):
        reject_institution(self.pending, self.reviewer, "Could not verify the school.")

        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, Institution.Status.REJECTED)
        self.assertEqual(self.pending.review_note, "Could not verify the school.")
        self.assertTrue(
            Institution.objects.filter(pk=self.pending.pk).exists(),
            "A rejected registration is retained, never deleted.",
        )

    def test_rejection_writes_to_no_tenant_table_at_all(self):
        """Unlike approval, rejection has no reason to touch `users`.

        The Owner stays inactive because it was created inactive — there is nothing
        to change.
        """
        with CaptureQueriesContext(connection) as captured:
            reject_institution(self.pending, self.reviewer, "Duplicate submission.")

        touched = tenant_tables_in(captured.captured_queries)
        self.assertEqual(touched, set())
        writes_to_users = [
            q["sql"]
            for q in captured.captured_queries
            if "users" in q["sql"].lower()
            and any(verb in q["sql"].lower() for verb in ("update", "insert", "delete"))
        ]
        self.assertEqual(writes_to_users, [])

    def test_rejection_requires_a_reason(self):
        self.sign_in_reviewer()
        response = self.client.post(
            reverse("platform_admin:reject_institution", args=[self.pending.pk]),
            {"reason": ""},
        )
        self.pending.refresh_from_db()
        self.assertEqual(self.pending.status, Institution.Status.PENDING)
        self.assertEqual(response.status_code, 302)

    def test_cannot_reject_an_approved_institution(self):
        approve_institution(self.pending, self.reviewer)
        with self.assertRaises(ApprovalError):
            reject_institution(self.pending, self.reviewer, "Changed my mind.")

    def test_rejected_owner_cannot_log_in(self):
        reject_institution(self.pending, self.reviewer, "Not a real school.")
        response = self.client.post(
            reverse("accounts:login"),
            {"username": "ada@pending.example", "password": "correct-horse-9"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)
