"""
Institution-scoped user accounts.




Every User belongs to exactly one Institution (docs/01_Architecture.md's
accepted constraint — no multi-campus in V1). The role field drives the
permission matrix in docs/04_Permission_Matrix.md.
"""

from django.contrib.auth.models import AbstractBaseUser
from django.db import models

from core.models import Institution

from .managers import UserManager


class User(AbstractBaseUser):
    """
    A staff account inside one school.

    Not TenantScopedModel: authentication must find a user by email *before*
    any institution is in context, and the scoped manager would return zero
    rows at that point. Tenant isolation for this table comes from the
    database Security Policy plus the explicit institution filter in the views
    that list users — never from the default manager.
    """

    class Role(models.TextChoices):
        OWNER = "Owner", "Owner"
        ADMINISTRATOR = "Administrator", "Administrator"
        BURSAR = "Bursar", "Bursar"
        # Inert in V1: present so the future Teacher/Parent module does not
        # need a migration. Grants no access anywhere.
        STAFF = "Staff", "Staff"

    institution = models.ForeignKey(
        Institution, on_delete=models.PROTECT, related_name="users"
    )

    email = models.EmailField(unique=True)
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=32, blank=True)
    role = models.CharField(max_length=16, choices=Role.choices)

    # False between registration and Super Admin approval, and for invited
    # users who have not yet set a password. LoginView checks both this and
    # the institution's status.
    is_active = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        db_table = "users"
        ordering = ("full_name",)
        constraints = [
            # One Owner per institution (docs/02_Database.md). Enforced here
            # rather than in a save() override so a bulk_create or a raw
            # fixture load cannot slip past it.
            models.UniqueConstraint(
                fields=("institution",),
                condition=models.Q(role="Owner"),
                name="uniq_owner_per_institution",
            )
        ]

    def __str__(self):
        return f"{self.full_name} <{self.email}> ({self.role})"

    @property
    def is_owner(self):
        return self.role == self.Role.OWNER

    @property
    def is_bursar(self):
        return self.role == self.Role.BURSAR

    @property
    def can_sign_in(self):
        """Active account inside an approved institution."""
        return self.is_active and self.institution.is_active_tenant
