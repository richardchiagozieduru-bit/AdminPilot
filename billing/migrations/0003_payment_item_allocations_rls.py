"""
Row-Level Security for payment_item_allocations table.
Binds to TenantIsolationPolicy with fn_TenantAccessPredicate.
"""

from django.db import connection, migrations


def apply_rls(apps, schema_editor):
    if connection.vendor != "microsoft":
        return

    with connection.cursor() as cursor:
        cursor.execute(
            """
            IF EXISTS (SELECT * FROM sys.security_policies WHERE name = 'TenantIsolationPolicy')
            BEGIN
                ALTER SECURITY POLICY dbo.TenantIsolationPolicy
                ADD FILTER PREDICATE dbo.fn_TenantAccessPredicate(institution_id) ON dbo.payment_item_allocations,
                ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) ON dbo.payment_item_allocations AFTER INSERT,
                ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) ON dbo.payment_item_allocations AFTER UPDATE,
                ADD BLOCK PREDICATE dbo.fn_TenantAccessPredicate(institution_id) ON dbo.payment_item_allocations BEFORE DELETE;
            END
            """
        )


def revert_rls(apps, schema_editor):
    if connection.vendor != "microsoft":
        return

    with connection.cursor() as cursor:
        cursor.execute(
            """
            IF EXISTS (SELECT * FROM sys.security_policies WHERE name = 'TenantIsolationPolicy')
            BEGIN
                ALTER SECURITY POLICY dbo.TenantIsolationPolicy
                DROP FILTER PREDICATE ON dbo.payment_item_allocations,
                DROP BLOCK PREDICATE ON dbo.payment_item_allocations AFTER INSERT,
                DROP BLOCK PREDICATE ON dbo.payment_item_allocations AFTER UPDATE,
                DROP BLOCK PREDICATE ON dbo.payment_item_allocations BEFORE DELETE;
            END
            """
        )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0002_paymentitemallocation"),
        ("core", "0002_tenant_rls"),
    ]

    operations = [
        migrations.RunPython(apply_rls, revert_rls),
    ]
