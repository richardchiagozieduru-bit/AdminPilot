from django.contrib.auth.models import BaseUserManager


class PlatformUserManager(BaseUserManager):
    use_in_migrations = True

    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("PlatformUser requires an email address.")
        email = self.normalize_email(email)
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, email, password=None, **extra_fields):
        """
        Every PlatformUser has identical capability, so this is create_user
        under the name `createsuperuser` expects.
        """
        extra_fields.setdefault("is_active", True)
        return self.create_user(email, password, **extra_fields)
