"""
Current-request institution context.

A ContextVar rather than threading.local: it is correct under ASGI, where one
thread interleaves many requests, and it does not leak between tasks. A
thread-local would let request B read request A's institution on a shared
worker thread.

Nothing here touches the database. Pushing the value into SQL Server's
SESSION_CONTEXT is core/middleware.py's job, and it must happen on every
request — see set_database_session_context().
"""

from contextlib import contextmanager
from contextvars import ContextVar

# The one place the current tenant lives. Default None means "no tenant", and
# every consumer must treat that as "return nothing", never "return all".
_current_institution_id: ContextVar[int | None] = ContextVar(
    "current_institution_id", default=None
)


def get_current_institution_id():
    return _current_institution_id.get()


def set_current_institution_id(institution_id):
    """Returns a token for reset(). Callers should prefer institution_context()."""
    return _current_institution_id.set(institution_id)


def reset_current_institution_id(token):
    _current_institution_id.reset(token)


@contextmanager
def institution_context(institution_id):
    """
    Scope a block of code to one institution.

    For management commands, Celery-style background work, and tests — the
    places with no request to derive context from. Resets on the way out even
    if the block raises, so an exception cannot leave context set for whatever
    runs next on this thread.
    """
    token = _current_institution_id.set(institution_id)
    try:
        yield institution_id
    finally:
        _current_institution_id.reset(token)


@contextmanager
def no_institution_context():
    """
    Explicitly clear tenant context.

    Used by platform (Super Admin) code paths so that a stray tenant-scoped
    query inside them returns nothing instead of inheriting whatever was set
    last.
    """
    token = _current_institution_id.set(None)
    try:
        yield
    finally:
        _current_institution_id.reset(token)
