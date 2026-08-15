from django.contrib.auth.models import BaseUserManager


class UserManager(BaseUserManager):
    """
    Institution-scoped user creation.

    No create_superuser: platform staff are PlatformUser objects
    (docs/02_Database.md), and `manage.py createsuperuser` must never mint an
    institution-scoped account — it has no way to pick an institution or role.
    """

    use_in_migrations = True

    def create_user(self, email, institution, role, password=None, **extra_fields):
        if not email:
            raise ValueError("User requires an email address.")
        if institution is None:
            raise ValueError("User requires an institution.")
        email = self.normalize_email(email)
        user = self.model(
            email=email, institution=institution, role=role, **extra_fields
        )
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_owner(self, email, institution, password=None, **extra_fields):
        """
        The Owner created by registration. Inactive until a Super Admin
        approves the institution — docs/01_Architecture.md.
        """
        extra_fields.setdefault("is_active", False)
        return self.create_user(
            email,
            institution=institution,
            role=self.model.Role.OWNER,
            password=password,
            **extra_fields,
        )
