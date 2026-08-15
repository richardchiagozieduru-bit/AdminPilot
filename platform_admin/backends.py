"""
Authentication backend for Super Admin accounts.

PlatformUser is not AUTH_USER_MODEL, so Django's session auth cannot load it
without a backend of its own. That is the whole reason this file exists.

Two things keep the separation from CR-004 real:

  - No auth_lookup_context() anywhere in here. `platform_users` carries no
    institution_id and is not bound to the tenant Security Policy
    (docs/02_Database.md), so it needs no exemption to read. If this file ever
    needs one, something has gone wrong with the separation.
  - request.session[BACKEND_SESSION_KEY] records which backend authenticated a
    session, so a PlatformUser session cannot be mistaken for an institution
    user's on a later request — they resolve through different backends and
    different models.
"""

from django.contrib.auth.backends import BaseBackend

from .models import PlatformUser


class PlatformUserBackend(BaseBackend):
    """Email + password against platform_admin.PlatformUser."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        email = username or kwargs.get("email")
        if not email or password is None:
            return None

        try:
            user = PlatformUser.objects.get(email__iexact=email)
        except PlatformUser.DoesNotExist:
            # Constant-time-ish: hash regardless, so an unknown platform email
            # is not distinguishable by response time.
            PlatformUser().set_password(password)
            return None

        if not user.check_password(password) or not user.is_active:
            return None
        return user

    def get_user(self, user_id):
        try:
            return PlatformUser.objects.get(pk=user_id)
        except PlatformUser.DoesNotExist:
            return None
