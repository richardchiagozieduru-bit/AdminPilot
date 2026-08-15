"""
Super Admin accounts.

Deliberately a separate model from accounts.User — not a role value on the
same table (docs/02_Database.md). A PlatformUser must be structurally unable
to carry an institution_id: it can never be swept into a tenant-scoped query
or the tenant isolation Security Policy.

This model is the entire scope of the Super Admin role: reviewing and
approving Institution registrations. There is no code path from here into any
institution's students, fees, or payments.
"""

from django.contrib.auth.models import AbstractBaseUser
from django.db import models

from .managers import PlatformUserManager


class PlatformUser(AbstractBaseUser):
    """
    AdminPilot staff account. Not an institution user; no institution FK.

    Authenticates against the same password hashers as User, but is never
    resolved into a tenant — see core/middleware.py.

    No PermissionsMixin: with django.contrib.admin uninstalled (CR-004) and
    access governed by the role matrix rather than Django permissions, the
    groups/user_permissions tables it adds would be dead schema. Every
    PlatformUser has exactly the same capability — approve or reject a
    registration — so there is nothing to differentiate with permissions.

    Authenticating this model needs its own backend (it is not
    AUTH_USER_MODEL); that lands with /platform/login/ in Phase 2.
    """

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = PlatformUserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "platform_users"

    def __str__(self):
        return f"PlatformUser: {self.email}"
