"""
Academic structure services.

Two things here that a view cannot express safely on its own:

  * "current" is exclusive. Exactly one Session and one Term may be current per
    institution, and the swap is a two-row change that must not be observable
    half-done — a reader that saw zero current terms would render a dashboard
    with no term at all.
  * The wizard creates a session, its first term, and the initial class list as
    one unit. A school left with a session and no term is stuck: enrollment and
    fee structures both need a term.
"""

from django.db import transaction

from core.services import write_audit_log

from .models import Class, ClassStatus, Session, Term


@transaction.atomic
def set_current_session(institution_id, session, actor=None, ip_address=None):
    """Make `session` the only current one for this institution.

    Unsets first, then sets: the reverse order would briefly leave two rows
    current, and "briefly" is long enough for a concurrent request to read it.
    """
    Session.unscoped.filter(institution_id=institution_id, is_current=True).exclude(
        pk=session.pk
    ).update(is_current=False)

    if not session.is_current:
        session.is_current = True
        session.save(update_fields=["is_current"])

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="session.set_current",
        summary=f"Set {session.name} as the current session",
        target_type="Session",
        target_id=session.pk,
        ip_address=ip_address,
    )
    return session


@transaction.atomic
def set_current_term(institution_id, term, actor=None, ip_address=None):
    """Make `term` the only current one, and its session the current session.

    A current term inside a non-current session is incoherent — the dashboard
    reads both — so this promotes the parent session too rather than leaving the
    Owner to notice and do it themselves.
    """
    Term.unscoped.filter(institution_id=institution_id, is_current=True).exclude(
        pk=term.pk
    ).update(is_current=False)

    if not term.is_current:
        term.is_current = True
        term.save(update_fields=["is_current"])

    if not term.session.is_current:
        set_current_session(
            institution_id, term.session, actor=actor, ip_address=ip_address
        )

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="term.set_current",
        summary=f"Set {term.session.name} — {term.name} as the current term",
        target_type="Term",
        target_id=term.pk,
        ip_address=ip_address,
    )
    return term


@transaction.atomic
def create_classes(institution_id, names, actor=None, ip_address=None):
    """Create classes from an ordered list of names.

    `order` follows the order the names arrived in, spaced by ten so an Owner can
    later slot a class between two others without renumbering the rest.

    One audit entry for the batch, not one per class — the same rule the bulk
    student import follows (docs/03_Views_and_Endpoints.md).
    """
    highest = (
        Class.unscoped.filter(institution_id=institution_id)
        .order_by("-order")
        .values_list("order", flat=True)
        .first()
        or 0
    )

    # A loop rather than bulk_create: the lists here are a dozen names at most,
    # both run in the same transaction, and bulk_create's OUTPUT-clause insert is
    # the kind of thing that behaves differently on the SQL Server backend for no
    # benefit at this size.
    created = [
        Class.unscoped.create(
            institution_id=institution_id,
            name=name,
            order=highest + (index + 1) * 10,
            status=ClassStatus.ACTIVE,
        )
        for index, name in enumerate(names)
    ]

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="class.created",
        summary=(
            f"Added {len(created)} class{'' if len(created) == 1 else 'es'}: "
            f"{', '.join(names)}"
        ),
        target_type="Class",
        detail={"names": list(names)},
        ip_address=ip_address,
    )
    return created


@transaction.atomic
def create_first_session_and_term(
    institution_id,
    session_form,
    term_name,
    term_start,
    term_end,
    actor=None,
    ip_address=None,
):
    """Wizard step 2: the first session plus its first term, both current.

    Takes a validated SessionForm rather than raw fields so the date-range and
    duplicate-name rules stay in one place. Returns (session, term).
    """
    session = session_form.save(commit=False)
    session.institution_id = institution_id
    session.is_current = True
    session.save()

    term = Term.unscoped.create(
        institution_id=institution_id,
        session=session,
        name=term_name,
        start_date=term_start,
        end_date=term_end,
        is_current=True,
    )

    write_audit_log(
        institution_id=institution_id,
        actor=actor,
        action="session.created",
        summary=f"Created session {session.name} with first term {term.name}",
        target_type="Session",
        target_id=session.pk,
        detail={"term": term.name},
        ip_address=ip_address,
    )
    return session, term
