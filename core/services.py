"""
Core services: audit logging and the per-institution number counters.

Both are things every other app needs and neither belongs in a view. The
counter function in particular is the one place a receipt or admission number
is allowed to come from — docs/02_Database.md is explicit that COUNT(*) and
MAX() are both wrong, because both reuse numbers after a delete and both race
under concurrent requests.
"""

from django.db import transaction

from core.models import AuditLog, InstitutionNumberSequence


def write_audit_log(
    *,
    institution_id,
    actor,
    action,
    summary,
    target_type="",
    target_id="",
    reason="",
    detail=None,
    ip_address=None,
):
    """Append one audit row.

    Call this inside the caller's `transaction.atomic()` block, never after it
    commits (CLAUDE.md rule 5) — an audit trail that can be missing for a
    mutation that succeeded is not an audit trail.

    `actor` may be None for system-originated entries. It is never a
    PlatformUser: the FK points at accounts.User, and platform accounts have no
    business writing into a tenant's log.
    """
    return AuditLog.unscoped.create(
        institution_id=institution_id,
        actor=actor if getattr(actor, "pk", None) else None,
        action=action,
        summary=summary[:255],
        target_type=target_type[:64],
        target_id=str(target_id)[:64],
        reason=reason,
        detail=detail,
        ip_address=ip_address,
    )


@transaction.atomic
def next_sequence_number(institution_id, kind, year):
    """Reserve and return the next number for one institution/kind/year.

    Takes a row lock on the counter (select_for_update) so two concurrent
    payments cannot read the same `last_number`. The caller must already be
    inside the transaction that writes the record this number belongs to,
    otherwise a rolled-back payment burns a receipt number.

    get_or_create then select_for_update rather than the reverse: the first
    receipt of a year has no row to lock yet.
    """
    InstitutionNumberSequence.unscoped.get_or_create(
        institution_id=institution_id, kind=kind, year=year
    )
    sequence = (
        InstitutionNumberSequence.unscoped.select_for_update()
        .filter(institution_id=institution_id, kind=kind, year=year)
        .get()
    )
    sequence.last_number += 1
    sequence.save(update_fields=["last_number", "updated_at"])
    return sequence.last_number


def format_sequence(code, year, number, width=6):
    """`[InstitutionCode]-[Year]-[Sequence]`, e.g. PERM-2026-000123."""
    return f"{code}-{year}-{number:0{width}d}"
