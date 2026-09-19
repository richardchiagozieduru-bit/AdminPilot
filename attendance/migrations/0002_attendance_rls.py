"""
Row-Level Security for attendance tables.

Adds the existing fn_TenantAccessPredicate filter and block predicates to the
two new attendance tables. Because SQL Server only allows one Security Policy
per schema, we must ALTER the existing TenantIsolationPolicy rather than
creating a new one.

The reverse drops the predicates from these tables but leaves the policy and
predicate functions intact (they still protect all other tenant tables).
"""

from django.db import migrations

ATTENDANCE_TABLES = [
    "class_attendance_registers",
    "attendance_records",
]


def build_add_predicates():
    """ALTER the existing policy to add predicates for attendance tables."""
    statements = []
    for table in ATTENDANCE_TABLES:
        statements.append(
            f"ALTER SECURITY POLICY dbo.TenantIsolationPolicy "
            f"ADD FILTER PREDICATE dbo.fn_TenantAccessPredicate(institution_id) "
            f"ON dbo.{table};"
        )
        statements.append(
            f"ALTER SECURITY POLICY dbo.TenantIsolationPolicy "
            f"ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) "
            f"ON dbo.{table} AFTER INSERT;"
        )
        statements.append(
            f"ALTER SECURITY POLICY dbo.TenantIsolationPolicy "
            f"ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) "
            f"ON dbo.{table} AFTER UPDATE;"
        )
    return statements


def build_drop_predicates():
    """Remove the attendance predicates from the policy."""
    statements = []
    for table in ATTENDANCE_TABLES:
        statements.append(
            f"ALTER SECURITY POLICY dbo.TenantIsolationPolicy "
            f"DROP FILTER PREDICATE ON dbo.{table};"
        )
        statements.append(
            f"ALTER SECURITY POLICY dbo.TenantIsolationPolicy "
            f"DROP BLOCK PREDICATE ON dbo.{table} AFTER INSERT;"
        )
        statements.append(
            f"ALTER SECURITY POLICY dbo.TenantIsolationPolicy "
            f"DROP BLOCK PREDICATE ON dbo.{table} AFTER UPDATE;"
        )
    return statements


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0001_initial"),
        # The RLS policy must already exist before we can ALTER it.
        ("core", "0002_tenant_rls"),
    ]

    operations = [
        migrations.RunSQL(
            sql=build_add_predicates(),
            reverse_sql=build_drop_predicates(),
        ),
    ]
