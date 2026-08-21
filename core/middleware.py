"""
Tenant context middleware.

This is the highest-risk file in the platform (docs/01_Architecture.md). The
whole RLS scheme rests on one invariant:

    SESSION_CONTEXT is set on EVERY request, immediately before that request's
    first query, and cleared when the request ends.

Why not the connection_created signal, which is the obvious place: Django
pools connections (CONN_MAX_AGE) and reuses them across requests. A signal
fires once, when the connection opens. Request A opens a connection and stamps
institution 1; request B is handed that same pooled connection and the signal
never fires again, so B runs against A's institution and RLS happily lets it
through. Same failure with SET SESSION_CONTEXT(..., @read_only = 1): the value
becomes immutable for the connection's lifetime, so the next request on it
cannot correct the stamp.

So: per-request, read_only = 0, and cleared in a finally block.
"""

import logging
from contextlib import contextmanager

from django.db import connection

from core.tenant import (
    get_current_institution_id,
    reset_current_institution_id,
    set_current_institution_id,
)

logger = logging.getLogger(__name__)

SESSION_CONTEXT_KEY = "institution_id"
AUTH_LOOKUP_KEY = "auth_lookup"


def _set_session_key(key, value):
    """EXEC sp_set_session_context for one key on the current connection.

    read_only is 0 deliberately. Locking a value would look safer and is in
    fact the bug described above — a pooled connection must be re-stampable.
    """
    with connection.cursor() as cursor:
        if value is None:
            cursor.execute(
                "EXEC sp_set_session_context @key = %s, @value = NULL, @read_only = 0",
                [key],
            )
        else:
            cursor.execute(
                "EXEC sp_set_session_context @key = %s, @value = %s, @read_only = 0",
                [key, value],
            )


def set_database_session_context(institution_id):
    """
    Stamp the current DB connection with the institution RLS will filter on.

    Passing None clears the stamp, which makes the RLS predicate match no rows.
    That is the intended failure mode: a request with no tenant resolved sees
    nothing rather than everything.
    """
    _set_session_key(
        SESSION_CONTEXT_KEY, None if institution_id is None else int(institution_id)
    )


@contextmanager
def auth_lookup_context():
    """
    Briefly exempt `users` reads from the tenant filter.

    Authentication has a bootstrap problem: finding the user by email is what
    tells us which institution to set, but under the tenant predicate that
    lookup returns zero rows because no institution is set yet. Same for
    AuthenticationMiddleware reloading request.user from the session on every
    subsequent request.

    So `users` carries a filter predicate that also passes when this flag is
    set (see core/migrations/0002_tenant_rls.py). The exemption is read-only —
    the BLOCK predicate on `users` uses the strict function, so this can never
    be used to write a row into another institution.

    Wrap the narrowest possible span: the credential lookup and the session
    user reload in accounts/backends.py (Phase 2), nothing else. Anything
    running inside this block can read users across institutions.
    """
    _set_session_key(AUTH_LOOKUP_KEY, 1)
    try:
        yield
    finally:
        _set_session_key(AUTH_LOOKUP_KEY, None)


@contextmanager
def institution_db_context(institution_id):
    """
    Run a block as though it were a request belonging to `institution_id`.

    Two writes in the platform happen outside any tenant request but must land
    inside a tenant. Registration inserts the Owner row for an institution
    nobody can log into yet, and Super Admin approval flips that Owner's
    is_active. Both hit the BLOCK PREDICATE on `users`, which passes only when
    SESSION_CONTEXT already names the row's own institution — so without this
    the insert is rejected by the database, correctly.

    Stamps both layers, the ContextVar so the scoped managers agree with the
    database, and restores whatever was set before on the way out. Restoring
    rather than clearing matters: used inside a normal request, clearing would
    leave the rest of that request unstamped.

    This is the only sanctioned way to write into a tenant from outside a
    request. Keep the block to the statement that needs it.
    """
    previous = get_current_institution_id()
    token = set_current_institution_id(institution_id)
    set_database_session_context(institution_id)
    try:
        yield institution_id
    finally:
        reset_current_institution_id(token)
        set_database_session_context(previous)


class TenantContextMiddleware:
    """
    Resolves the request's institution and stamps it on both the Python context
    and the database connection.

    Placed after AuthenticationMiddleware — it needs request.user — and before
    any middleware or view that queries tenant data.

    PlatformUsers are never stamped. A Super Admin has no institution, and
    giving them one would put platform code inside a tenant's RLS scope, which
    is exactly the separation docs/02_Database.md is protecting.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        institution_id = self.resolve_institution_id(request)
        token = set_current_institution_id(institution_id)

        try:
            # Must precede the view. Anything that queries before this line
            # runs unstamped, and with the clear-on-exit below that means it
            # runs with no tenant rather than the wrong one.
            set_database_session_context(institution_id)
            if request.path.startswith("/admin/"):
                _set_session_key(AUTH_LOOKUP_KEY, 1)
            request.institution_id = institution_id
            response = self.get_response(request)
        finally:
            # Clear before the connection returns to the pool. If the next
            # request somehow fails to stamp, it inherits "no tenant" and sees
            # nothing, instead of inheriting this request's institution.
            try:
                set_database_session_context(None)
                # Belt and braces: auth_lookup_context clears this itself, but
                # a connection must never go back to the pool with the users
                # exemption still open.
                _set_session_key(AUTH_LOOKUP_KEY, None)
            except Exception:
                # A broken connection cannot leak context — it is being torn
                # down. Log and move on; never mask the real exception.
                logger.warning("Could not clear SESSION_CONTEXT", exc_info=True)
            reset_current_institution_id(token)

        return response

    def resolve_institution_id(self, request):
        """
        The only place a request's tenant is decided.

        Derived from the authenticated user's own row — never from a URL
        segment, query parameter, header, or form field. If a client could
        name its institution, tenant isolation would be a client-side check.
        """
        user = getattr(request, "user", None)
        if user is None:
            return None

        # request.user is lazy: touching it here is what triggers the session
        # user reload, and that SELECT on `users` runs before any institution
        # is stamped. It needs the exemption or it returns zero rows and every
        # request looks anonymous.
        with auth_lookup_context():
            if not user.is_authenticated:
                return None
            # PlatformUser has no institution attribute at all; the getattr
            # keeps this branch honest without an isinstance import cycle.
            return getattr(user, "institution_id", None)
