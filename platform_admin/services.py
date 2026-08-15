"""
Approval and rejection.

Read the import list before anything else: `core.models.Institution` and
`accounts.models.User`, and nothing from academic, students, or billing.
docs/02_Database.md's checklist item 5 asks for Super Admin's isolation to be
verifiable structurally, and this file is the whole surface where platform code
touches institution tables — a tenant-model import appearing here is the thing
to catch in review.

Institution itself is not tenant-scoped (it *is* the tenant, no institution_id
column, not bound to the Security Policy), so reading and updating it is not a
cross-tenant read.

`users` is the one exception, and the spec requires it: "Sets status='approved',
activates the Owner account". That write is narrowed to exactly one row, by
role, inside institution_db_context so the database's own BLOCK predicate has
to agree it belongs to that institution.
"""

from django.db import transaction
from django.utils import timezone

from accounts.models import User
from core.middleware import institution_db_context
from core.models import Institution


class ApprovalError(Exception):
    """A transition that does not apply to the institution's current status."""


@transaction.atomic
def approve_institution(institution, reviewer):
    """pending → approved, and activate the Owner.

    Idempotent by refusing rather than repeating: approving twice would rewrite
    reviewed_by/reviewed_at and lose who actually made the call.
    """
    institution = (
        Institution.objects.select_for_update().get(pk=institution.pk)
    )
    if institution.status == Institution.Status.APPROVED:
        raise ApprovalError(f"{institution.name} is already approved.")

    institution.status = Institution.Status.APPROVED
    institution.reviewed_by = reviewer
    institution.reviewed_at = timezone.now()
    institution.review_note = ""
    institution.save(
        update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"]
    )

    # The only write into a tenant-scoped table in this entire app. Filtered to
    # the Owner of this one institution; the BLOCK predicate on `users` rejects
    # anything else even if this filter were wrong.
    with institution_db_context(institution.pk):
        activated = User.objects.filter(
            institution_id=institution.pk, role=User.Role.OWNER
        ).update(is_active=True, updated_at=timezone.now())

    if not activated:
        # Registration creates institution and Owner in one transaction, so a
        # missing Owner means data was hand-edited. Roll back rather than leave
        # an approved institution nobody can sign in to.
        raise ApprovalError(
            f"{institution.name} has no Owner account to activate. "
            f"Approval cancelled."
        )

    return institution


@transaction.atomic
def reject_institution(institution, reviewer, reason):
    """pending → rejected. The row is retained, never deleted.

    Same soft-state philosophy as the rest of the schema
    (docs/01_Architecture.md step 4) — a rejected registration is a record.

    The Owner row is left inactive rather than touched: it was created inactive
    and never activated, so there is nothing to undo, and this keeps rejection
    a zero-write path into `users`.
    """
    if not reason or not reason.strip():
        raise ApprovalError("A rejection reason is required.")

    institution = Institution.objects.select_for_update().get(pk=institution.pk)
    if institution.status == Institution.Status.APPROVED:
        # Rejecting a live school would strand its data behind a status gate.
        # Suspension is the operation for that, and it is not in V1 scope.
        raise ApprovalError(
            f"{institution.name} is already approved. Rejecting an approved "
            f"institution is not supported."
        )

    institution.status = Institution.Status.REJECTED
    institution.reviewed_by = reviewer
    institution.reviewed_at = timezone.now()
    institution.review_note = reason.strip()
    institution.save(
        update_fields=["status", "reviewed_by", "reviewed_at", "review_note", "updated_at"]
    )
    return institution
