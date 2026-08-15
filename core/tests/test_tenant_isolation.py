"""
Tenant isolation verification — the Phase 1 gate.

One test per item in docs/02_Database.md's checklist (lines 90-99). That
checklist must pass before any application code is built on top of it, so
these are written now and run the moment a SQL Server instance is available.

These require a real SQL Server: they verify Row-Level Security, which is a
database feature. SQLite would pass them all vacuously and prove nothing —
that failure mode is worse than not running them, so test_rls_is_active()
asserts the policy actually exists before the rest are meaningful.

Run: manage.py test core.tests.test_tenant_isolation
"""

from django.db import DatabaseError, connection, transaction
from django.test import TestCase

from academic.models import Class, Session, Term
from accounts.models import User
from core.middleware import auth_lookup_context, set_database_session_context
from core.models import Institution
from core.tenant import institution_context
from students.models import Student


def session_context_institution():
    """Read back what SESSION_CONTEXT currently holds, as the DB sees it."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT CAST(SESSION_CONTEXT(N'institution_id') AS INT)")
        return cursor.fetchone()[0]


class TenantIsolationTests(TestCase):
    """Two institutions, populated with the unscoped manager so setup itself
    is not subject to the thing under test."""

    @classmethod
    def setUpTestData(cls):
        cls.inst_a = Institution.objects.create(
            name="Alpha Academy", code="ALPHA", status=Institution.Status.APPROVED
        )
        cls.inst_b = Institution.objects.create(
            name="Beta School", code="BETA", status=Institution.Status.APPROVED
        )

        for inst, tag in ((cls.inst_a, "A"), (cls.inst_b, "B")):
            with institution_context(inst.id):
                set_database_session_context(inst.id)
                Student.objects.create(
                    institution=inst,
                    first_name=f"Student{tag}",
                    last_name="Test",
                    gender="male",
                    date_of_birth="2010-01-01",
                    admission_number=f"{inst.code}-2026-000001",
                    guardian_name="Guardian",
                    guardian_phone="08000000000",
                )
                User.objects.create_user(
                    email=f"owner.{tag.lower()}@example.com",
                    institution=inst,
                    role=User.Role.OWNER,
                    full_name=f"Owner {tag}",
                    password="test-pass-12345",
                )
        set_database_session_context(None)

    def tearDown(self):
        set_database_session_context(None)

    def test_rls_is_active(self):
        """Guard test: everything below is vacuous if the policy is missing."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT is_enabled FROM sys.security_policies "
                "WHERE name = 'TenantIsolationPolicy'"
            )
            row = cursor.fetchone()
        self.assertIsNotNone(
            row, "TenantIsolationPolicy does not exist — RLS migration not applied."
        )
        self.assertTrue(row[0], "TenantIsolationPolicy exists but is disabled.")

    def test_1_cross_tenant_read_blocked(self):
        """Checklist 1 — A's session sees zero of B's rows."""
        set_database_session_context(self.inst_a.id)

        # unscoped deliberately: this must be the database refusing, not the
        # application manager filtering. If RLS is the only thing standing
        # between A and B's data, this is the test that proves it.
        visible = Student.unscoped.all()
        self.assertEqual(visible.count(), 1)
        self.assertEqual(visible.first().institution_id, self.inst_a.id)

        # User is not a TenantScopedModel — its manager does no filtering, so
        # RLS is the only thing scoping it. Inside the auth exemption both
        # institutions' rows are visible; outside it, only A's.
        with auth_lookup_context():
            self.assertEqual(User.objects.count(), 2)

        self.assertEqual(
            User.objects.filter(institution=self.inst_b).count(),
            0,
            "Institution A's session can read Institution B's user rows.",
        )

    def test_the_auth_exemption_is_read_only(self):
        """CR-007's central claim: the exemption cannot be used to write.

        `users` carries the lenient FILTER predicate so a login can find an
        account before it knows the institution, but both BLOCK predicates on
        that table use the strict function. Inside the exemption B's row is
        visible — which is exactly what makes this worth pinning, because a
        visible row is a row an UPDATE can match.
        """
        set_database_session_context(self.inst_a.id)

        with auth_lookup_context():
            with self.assertRaises(DatabaseError, msg="INSERT into another tenant"):
                with transaction.atomic():
                    User.objects.create_user(
                        email="smuggled@example.com",
                        institution=self.inst_b,
                        role=User.Role.OWNER,
                        full_name="Smuggled Owner",
                        password="test-pass-12345",
                    )

            self.assertEqual(
                User.objects.filter(institution=self.inst_b).count(),
                1,
                "The row has to be visible here or the UPDATE below proves nothing.",
            )
            with self.assertRaises(DatabaseError, msg="UPDATE of another tenant"):
                with transaction.atomic():
                    User.objects.filter(institution=self.inst_b).update(
                        full_name="Renamed by Institution A"
                    )

        with auth_lookup_context():
            self.assertEqual(
                User.objects.get(institution=self.inst_b).full_name,
                "Owner B",
                "B's row was modified from A's session.",
            )

    def test_the_auth_exemption_does_not_extend_past_users(self):
        """The flag is on the `users` predicate alone.

        Every other tenant table keeps the strict filter, so a code path that
        leaves the exemption open longer than it should still cannot read
        anything else across tenants — that is what keeps the blast radius of a
        misplaced auth_lookup_context() to one table.
        """
        with auth_lookup_context():
            set_database_session_context(self.inst_a.id)
            self.assertEqual(Student.unscoped.count(), 1)
            self.assertEqual(Student.unscoped.first().institution_id, self.inst_a.id)

            set_database_session_context(None)
            self.assertEqual(
                Student.unscoped.count(),
                0,
                "The auth exemption leaked into `students` — it must apply to "
                "`users` and nothing else.",
            )

    def test_2_cross_tenant_write_blocked(self):
        """Checklist 2 — the BLOCK predicate rejects the write outright.

        Not 'silently corrected', not 'allowed then filtered' — the database
        raises. Wrapped in atomic() because the failure aborts the transaction.
        """
        set_database_session_context(self.inst_a.id)

        with self.assertRaises(DatabaseError):
            with transaction.atomic():
                Student.unscoped.create(
                    institution=self.inst_b,  # someone else's tenant
                    first_name="Smuggled",
                    last_name="Row",
                    gender="female",
                    date_of_birth="2011-05-05",
                    admission_number="BETA-2026-000999",
                    guardian_name="Guardian",
                    guardian_phone="08000000000",
                )

        set_database_session_context(self.inst_b.id)
        self.assertFalse(
            Student.unscoped.filter(first_name="Smuggled").exists(),
            "Cross-tenant insert was not blocked.",
        )

    def test_3_no_session_context_returns_zero_rows(self):
        """Checklist 3 — fail closed, and quietly.

        No exception: an error message is itself a disclosure. Absence of
        context means absence of rows.
        """
        set_database_session_context(None)

        for model in (Student, Class, Session, Term):
            self.assertEqual(
                model.unscoped.count(),
                0,
                f"{model.__name__} returned rows with no session context set.",
            )

    def test_4_connection_reuse_does_not_leak_context(self):
        """Checklist 4 — the highest-risk scenario in docs/01_Architecture.md.

        Request A (institution 1) then Request B (institution 2) on the *same*
        pooled connection. Django's TestCase reuses one connection throughout,
        which is exactly the condition being tested — sequential stamps on one
        physical connection.
        """
        set_database_session_context(self.inst_a.id)
        self.assertEqual(session_context_institution(), self.inst_a.id)
        first = list(Student.unscoped.values_list("institution_id", flat=True))

        # Simulated end of request A / start of request B, same connection.
        set_database_session_context(None)
        set_database_session_context(self.inst_b.id)

        self.assertEqual(
            session_context_institution(),
            self.inst_b.id,
            "SESSION_CONTEXT did not update on a reused connection — "
            "this is the pooled-connection leak.",
        )
        second = list(Student.unscoped.values_list("institution_id", flat=True))

        self.assertEqual(first, [self.inst_a.id])
        self.assertEqual(second, [self.inst_b.id])
        self.assertNotIn(self.inst_a.id, second, "Stale context leaked into request B.")

    def test_5_platform_users_are_outside_tenancy(self):
        """Checklist 5 — structural, not 'the UI doesn't show it'.

        PlatformUser has no institution_id column at all, so no Super Admin row
        can be swept into a tenant query. Asserted against the schema, because
        a model-level assertion would pass even if a migration added the column.
        """
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) FROM sys.columns "
                "WHERE object_id = OBJECT_ID('dbo.platform_users') "
                "AND name = 'institution_id'"
            )
            self.assertEqual(
                cursor.fetchone()[0],
                0,
                "platform_users has an institution_id column — Super Admin is "
                "no longer structurally outside tenancy.",
            )

            cursor.execute(
                "SELECT COUNT(*) FROM sys.security_predicates p "
                "JOIN sys.security_policies s ON s.object_id = p.object_id "
                "WHERE s.name = 'TenantIsolationPolicy' "
                "AND p.target_object_id = OBJECT_ID('dbo.platform_users')"
            )
            self.assertEqual(
                cursor.fetchone()[0], 0, "platform_users is under the tenant policy."
            )

    def test_6_institution_status_gates_sign_in(self):
        """Checklist 6 — a pending/rejected institution's Owner cannot sign in,
        even though the row and credentials are valid."""
        pending = Institution.objects.create(
            name="Gamma College", code="GAMMA", status=Institution.Status.PENDING
        )
        with institution_context(pending.id):
            set_database_session_context(pending.id)
            owner = User.objects.create_owner(
                email="owner.gamma@example.com",
                institution=pending,
                full_name="Owner Gamma",
                password="test-pass-12345",
            )
            owner.is_active = True  # even with the account itself enabled
            owner.save()

            self.assertFalse(
                owner.can_sign_in,
                "Owner of a pending institution can sign in.",
            )

            for status in (Institution.Status.REJECTED, Institution.Status.SUSPENDED):
                pending.status = status
                pending.save()
                owner.refresh_from_db()
                self.assertFalse(
                    owner.can_sign_in, f"Owner can sign in while {status}."
                )

            pending.status = Institution.Status.APPROVED
            pending.save()
            owner.refresh_from_db()
            self.assertTrue(
                owner.can_sign_in,
                "Owner of an approved institution cannot sign in — gate is "
                "inverted or always-false.",
            )


class TenantManagerTests(TestCase):
    """The second layer (core/managers.py). Defense in depth — the tests above
    are the ones that prove isolation."""

    @classmethod
    def setUpTestData(cls):
        cls.inst = Institution.objects.create(
            name="Delta School", code="DELTA", status=Institution.Status.APPROVED
        )

    def tearDown(self):
        set_database_session_context(None)

    def test_default_manager_returns_nothing_without_context(self):
        """No tenant in context must mean no rows — never all rows."""
        self.assertEqual(Student.objects.count(), 0)
        self.assertEqual(Class.objects.count(), 0)

    def test_default_manager_scopes_to_current_institution(self):
        with institution_context(self.inst.id):
            set_database_session_context(self.inst.id)
            Class.objects.create(institution=self.inst, name="JSS1")
            self.assertEqual(Class.objects.count(), 1)

        set_database_session_context(self.inst.id)
        self.assertEqual(
            Class.objects.count(),
            0,
            "Manager returned rows after the institution context exited.",
        )
