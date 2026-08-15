"""
Row-Level Security: predicate functions + the tenant isolation Security Policy.

Hand-written on purpose. Nothing in models.py produces this — makemigrations
cannot see it, and docs/07_Implementation_Roadmap.md flags it as the step most
likely to be skipped. RLS is the enforcement mechanism for tenant isolation,
not a backstop (docs/01_Architecture.md ADR-001); the TenantScopedManager in
core/managers.py is the second layer.

Depends on every app's 0001_initial because the policy binds to tables that
must already exist. If your generated initial migrations are named differently,
fix the dependencies list below to match.

Each statement is its own string: CREATE FUNCTION and CREATE SECURITY POLICY
must each begin a batch, and `GO` is an SSMS client directive, not T-SQL — it
would raise a syntax error if embedded here.
"""

from django.db import migrations

# Every table carrying institution_id. Deliberately excluded:
#   institutions   — it *is* the tenant; no institution_id column to filter on
#   platform_users — Super Admin, structurally outside tenancy (docs/02_Database.md)
#   django_*       — framework tables, no tenant data
TENANT_TABLES = [
    # core
    "institution_number_sequences",
    "audit_logs",
    # academic
    "sessions",
    "terms",
    "classes",
    # students
    "students",
    "student_enrollments",
    "bulk_import_batches",
    "bulk_import_rows",
    # billing
    "fee_structures",
    "fee_structure_items",
    "student_fee_assignments",
    "payments",
    "receipts",
    "credit_transactions",
]

# `users` is handled separately — see AUTH_LOOKUP_PREDICATE below.
USERS_TABLE = "users"


# The predicate every tenant table reads through. No session context set means
# no row matches, which is checklist item 3 in docs/02_Database.md: fail closed,
# zero rows, no error that leaks schema detail.
CREATE_PREDICATE = """
CREATE FUNCTION dbo.fn_TenantAccessPredicate(@institution_id INT)
RETURNS TABLE
WITH SCHEMABINDING
AS
RETURN
    SELECT 1 AS fn_result
    WHERE @institution_id = CAST(SESSION_CONTEXT(N'institution_id') AS INT);
"""

# Reads against `users` need one exemption. Authentication has to find a user
# by email *before* any institution is known — that lookup is what tells us
# which institution to set. Under the strict predicate it returns zero rows and
# nobody can ever log in.
#
# So: a second key, set only around the credential lookup itself
# (core.middleware.auth_lookup_context) and cleared immediately after. Still
# fails closed by default — an ordinary request sets neither key and sees
# nothing.
#
# This exemption is READ-ONLY. The BLOCK predicate on `users` uses the strict
# function below, so the auth flag can never be used to write a row into
# another institution.
CREATE_AUTH_LOOKUP_PREDICATE = """
CREATE FUNCTION dbo.fn_TenantAccessPredicateAuthLookup(@institution_id INT)
RETURNS TABLE
WITH SCHEMABINDING
AS
RETURN
    SELECT 1 AS fn_result
    WHERE @institution_id = CAST(SESSION_CONTEXT(N'institution_id') AS INT)
       OR CAST(SESSION_CONTEXT(N'auth_lookup') AS BIT) = 1;
"""


def build_security_policy():
    """Assemble the single CREATE SECURITY POLICY statement.

    Built from a list rather than written out because 16 tables x 3 predicates
    is 48 near-identical clauses, and a typo in one of them is a silent hole
    rather than an error.
    """
    clauses = []

    for table in TENANT_TABLES:
        clauses.append(
            f"ADD FILTER PREDICATE dbo.fn_TenantAccessPredicate(institution_id) "
            f"ON dbo.{table}"
        )
        clauses.append(
            f"ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) "
            f"ON dbo.{table} AFTER INSERT"
        )
        clauses.append(
            f"ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) "
            f"ON dbo.{table} AFTER UPDATE"
        )

    # Lenient predicate for reads, strict for writes.
    clauses.append(
        f"ADD FILTER PREDICATE dbo.fn_TenantAccessPredicateAuthLookup(institution_id) "
        f"ON dbo.{USERS_TABLE}"
    )
    clauses.append(
        f"ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) "
        f"ON dbo.{USERS_TABLE} AFTER INSERT"
    )
    clauses.append(
        f"ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) "
        f"ON dbo.{USERS_TABLE} AFTER UPDATE"
    )

    return (
        "CREATE SECURITY POLICY dbo.TenantIsolationPolicy\n"
        + ",\n".join(clauses)
        + "\nWITH (STATE = ON);"
    )


DROP_SECURITY_POLICY = "DROP SECURITY POLICY IF EXISTS dbo.TenantIsolationPolicy;"
DROP_PREDICATE = "DROP FUNCTION IF EXISTS dbo.fn_TenantAccessPredicate;"
DROP_AUTH_LOOKUP_PREDICATE = (
    "DROP FUNCTION IF EXISTS dbo.fn_TenantAccessPredicateAuthLookup;"
)


class Migration(migrations.Migration):

    dependencies = [
        # Points at the migration that puts the institution_id column on each
        # table, not merely the one that creates the table. academic and
        # accounts split into 0002_initial to break a circular FK, and the
        # policy cannot bind to a column that does not exist yet.
        ("core", "0001_initial"),
        ("accounts", "0002_initial"),
        ("academic", "0002_initial"),
        ("students", "0001_initial"),
        ("billing", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                CREATE_PREDICATE,
                CREATE_AUTH_LOOKUP_PREDICATE,
                build_security_policy(),
            ],
            # Policy first: the functions are schema-bound to it and cannot be
            # dropped while it references them.
            reverse_sql=[
                DROP_SECURITY_POLICY,
                DROP_PREDICATE,
                DROP_AUTH_LOOKUP_PREDICATE,
            ],
        ),
    ]
