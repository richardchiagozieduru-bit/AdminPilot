"""
Authentication backend for institution users.

Exists for one reason: every read of `users` has to happen inside
core.middleware.auth_lookup_context(). The tenant Security Policy filters that
table like any other, and at login time no institution is set yet — the lookup
that would tell us which institution to set is the one being filtered. Django's
stock ModelBackend queries the table directly and would find nobody, on every
login, forever.

So both halves of the backend contract are wrapped:

  authenticate()  — the credential lookup
  get_user()      — AuthenticationMiddleware reloading request.user from the
                    session, which runs on every single request, not just login

The exemption is read-only. `users` carries the strict predicate for
INSERT/UPDATE (core/migrations/0002_tenant_rls.py), so nothing reachable from
here can write across institutions.

Status is deliberately not checked here. A backend returning None is
indistinguishable from a wrong password, and docs/08_UI_UX.md wants a pending
school told exactly that. LoginForm makes that distinction instead.
"""

from django.contrib.auth.backends import BaseBackend

from core.middleware import auth_lookup_context, institution_db_context

from .models import User


class InstitutionUserBackend(BaseBackend):
    """Email + password against accounts.User."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        email = username or kwargs.get("email")
        if not email or password is None:
            return None

        with auth_lookup_context():
            try:
                user = User.objects.get(email__iexact=email)
            except User.DoesNotExist:
                # Hash anyway. Returning early on an unknown email makes the
                # response measurably faster than a wrong password on a known
                # one, which turns login into an account-enumeration oracle.
                User().set_password(password)
                return None

        with institution_db_context(user.institution_id):
            if not user.check_password(password):
                return None

        # Returned regardless of is_active. can_sign_in() is checked by the
        # form so the user gets the real reason; ModelBackend's silent
        # is_active rejection would collapse it back to "invalid credentials".
        return user

    def get_user(self, user_id):
        with auth_lookup_context():
            try:
                return User.objects.select_related("institution").get(pk=user_id)
            except User.DoesNotExist:
                return None

    def user_can_authenticate(self, user):
        """Overrides BaseBackend's is_active gate — see authenticate()."""
        return True
