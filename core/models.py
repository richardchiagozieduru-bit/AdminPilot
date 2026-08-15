"""
Core tenancy models: the Institution (the tenant itself), the abstract base
every tenant-scoped table inherits, the per-institution number counters, and
the audit log.

Institution deliberately does NOT inherit TenantScopedModel — it is the tenant,
not a row inside one, and it carries no institution_id.
"""

from django.conf import settings
from django.db import models

from core.managers import TenantScopedManager


class Institution(models.Model):
    """
    One school. Created by self-serve registration in a pending state; only a
    Super Admin approval flips it to approved. Nothing inside the institution
    is reachable until then — see docs/05_Scope_Boundary.md.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending approval"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        SUSPENDED = "suspended", "Suspended"

    class Type(models.TextChoices):
        NURSERY = "nursery", "Nursery"
        PRIMARY = "primary", "Primary"
        SECONDARY = "secondary", "Secondary"
        COMBINED = "combined", "Combined"
        OTHER = "other", "Other"

    name = models.CharField(max_length=255)
    # Short human-readable identifier used as the prefix in admission and
    # receipt numbers. Immutable once receipts exist, or historical receipt
    # numbers stop matching the institution that issued them.
    code = models.CharField(max_length=16, unique=True)
    type = models.CharField(max_length=16, choices=Type.choices, default=Type.COMBINED)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )

    email = models.EmailField()
    phone = models.CharField(max_length=32, blank=True)
    address = models.TextField(blank=True)
    timezone = models.CharField(max_length=64, default="Africa/Lagos")
    currency_code = models.CharField(max_length=3, default="NGN")

    # Approval trail. reviewed_by points at a PlatformUser, not a User: only
    # Super Admins can approve, and they live outside the tenant.
    reviewed_by = models.ForeignKey(
        "platform_admin.PlatformUser",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reviewed_institutions",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(
        blank=True, help_text="Reason shown to the applicant on rejection."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "institutions"
        ordering = ("name",)

    def __str__(self):
        return f"{self.name} ({self.code})"

    @property
    def is_active_tenant(self):
        return self.status == self.Status.APPROVED


class TenantScopedModel(models.Model):
    """
    Base for every table carrying an institution_id.

    Subclasses get two managers:
      objects  — filtered to the current request's institution, fails closed
      unscoped — unfiltered, for platform code and management commands

    base_manager_name is unscoped so that following a ForeignKey never trips
    the scoped filter. Related-object traversal must not depend on request
    state, or a legitimately-loaded parent can fail to find its own children.
    """

    institution = models.ForeignKey(
        Institution, on_delete=models.PROTECT, related_name="%(class)ss"
    )

    objects = TenantScopedManager()
    unscoped = models.Manager()

    class Meta:
        abstract = True
        base_manager_name = "unscoped"


class InstitutionNumberSequence(TenantScopedModel):
    """
    Per-institution, per-year, per-kind counter for admission and receipt
    numbers. A row here is the authority for the next number.

    Never derive a number from COUNT(*) or MAX(): both reuse numbers after a
    delete and both race under concurrency. Callers take a row lock
    (select_for_update) inside the same transaction that writes the record the
    number belongs to. See docs/02_Database.md.
    """

    class Kind(models.TextChoices):
        ADMISSION = "admission", "Admission number"
        RECEIPT = "receipt", "Receipt number"

    kind = models.CharField(max_length=16, choices=Kind.choices)
    year = models.PositiveIntegerField()
    last_number = models.PositiveIntegerField(default=0)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "institution_number_sequences"
        constraints = [
            models.UniqueConstraint(
                fields=("institution", "kind", "year"),
                name="uniq_sequence_per_kind_year",
            )
        ]

    def __str__(self):
        return f"{self.institution_id}/{self.kind}/{self.year} @ {self.last_number}"


class AuditLog(TenantScopedModel):
    """
    Append-only record of sensitive mutations. Written server-side inside the
    same transaction as the mutation it describes, never reported by a client.

    No updates or deletes at the application layer: there is no update path in
    any service, and nothing should ever add one. The log is also the source
    for the Student Profile activity timeline, so `summary` holds a
    pre-formatted plain-language line — docs/03_Views_and_Endpoints.md.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audit_entries",
        help_text="Null once a user is deleted; the log entry survives them.",
    )
    action = models.CharField(
        max_length=64, db_index=True, help_text="Dotted verb, e.g. 'payment.recorded'."
    )

    # Loose reference rather than a real FK: audit rows outlive the records
    # they describe, and a FK would either block deletion or cascade the
    # history away.
    target_type = models.CharField(max_length=64, blank=True)
    target_id = models.CharField(max_length=64, blank=True)

    summary = models.CharField(max_length=255)
    detail = models.JSONField(null=True, blank=True)
    reason = models.TextField(
        blank=True, help_text="Required for reversals and fee adjustments."
    )

    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "audit_logs"
        ordering = ("-created_at",)
        indexes = [
            models.Index(
                fields=("institution", "target_type", "target_id"),
                name="ix_audit_target",
            ),
            models.Index(fields=("institution", "actor"), name="ix_audit_actor"),
        ]

    def __str__(self):
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.action}"
