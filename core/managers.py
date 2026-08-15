"""
Managers for tenant-scoped models.

Second layer only. RLS in the database is the floor — see docs/01_Architecture.md.
The point of this layer is to make accidental cross-tenant queries fail loudly
in development instead of relying on RLS to silently return zero rows.
"""

from django.db import models

from core.tenant import get_current_institution_id


class TenantScopedQuerySet(models.QuerySet):
    def for_institution(self, institution_id):
        """Explicit scope, for code that legitimately runs outside a request."""
        return self.filter(institution_id=institution_id)


class TenantScopedManager(models.Manager.from_queryset(TenantScopedQuerySet)):
    """
    Filters every query by the institution owning the current request.

    Fails closed: with no institution in context, returns an empty queryset
    rather than every row on the platform. Management commands and platform
    (Super Admin) code that must span institutions use `unscoped` instead,
    which is deliberately more verbose so it stands out in review.
    """

    def get_queryset(self):
        queryset = super().get_queryset()
        institution_id = get_current_institution_id()
        if institution_id is None:
            return queryset.none()
        return queryset.filter(institution_id=institution_id)
