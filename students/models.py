"""
Students, their enrollments, and the bulk-import staging tables.

Two invariants from docs/02_Database.md shape this module:

- StudentEnrollment is append-only. A student's class assignment is a new
  row, never an UPDATE — the history is what later powers promotion.
- Admission numbers are platform-generated from a per-institution counter
  ([Code]-[Year]-[Seq]), never school-supplied, which is why the import
  template has no admission number column.

Field names mirror the bulk-import template columns (First Name, Middle Name,
Last Name, ...) so staging rows and live rows share one vocabulary.
"""

from django.conf import settings
from django.db import models

from academic.models import Class, Session, Term
from core.models import TenantScopedModel


class Gender(models.TextChoices):
    MALE = "male", "Male"
    FEMALE = "female", "Female"


class StudentStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"


class Student(TenantScopedModel):
    first_name = models.CharField(max_length=100)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100)
    gender = models.CharField(max_length=10, choices=Gender.choices)
    date_of_birth = models.DateField()
    admission_number = models.CharField(max_length=32)

    # Guardian / contact — the required/optional split matches the import
    # template: guardian_name and guardian_phone are required at minimum.
    father_name = models.CharField(max_length=100, blank=True)
    mother_name = models.CharField(max_length=100, blank=True)
    guardian_name = models.CharField(max_length=100)
    guardian_phone = models.CharField(max_length=20)
    guardian_email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    # Not part of bulk import (docs/02_Database.md — no file-embedding in
    # Excel); added individually per student later.
    photo = models.ImageField(upload_to="student_photos/", blank=True, null=True)

    date_of_admission = models.DateField(null=True, blank=True)

    # Cached running total, kept in sync at write time by the payment service
    # so the UI never sums the credit ledger on page load (docs/02_Database.md).
    credit_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    status = models.CharField(
        max_length=10, choices=StudentStatus.choices, default=StudentStatus.ACTIVE
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "students"
        ordering = ("last_name", "first_name")
        constraints = [
            models.UniqueConstraint(
                fields=("institution", "admission_number"),
                name="uniq_admission_number",
            )
        ]

    def __str__(self):
        return f"{self.full_name} ({self.admission_number})"

    @property
    def full_name(self):
        return " ".join(
            part for part in (self.first_name, self.middle_name, self.last_name) if part
        )


class StudentEnrollment(TenantScopedModel):
    """A student's class assignment for one session/term.

    Append-only: a class change inserts a new row; nothing updates an existing
    enrollment. `klass` is named this way because `class` is a Python keyword.
    """

    student = models.ForeignKey(
        Student, on_delete=models.PROTECT, related_name="enrollments"
    )
    klass = models.ForeignKey(
        Class, on_delete=models.PROTECT, related_name="enrollments"
    )
    session = models.ForeignKey(
        Session, on_delete=models.PROTECT, related_name="enrollments"
    )
    term = models.ForeignKey(
        Term, on_delete=models.PROTECT, related_name="enrollments"
    )
    enrolled_at = models.DateTimeField(auto_now_add=True)
    enrolled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )

    class Meta:
        db_table = "student_enrollments"
        ordering = ("-enrolled_at",)
        indexes = [
            models.Index(
                fields=("student", "session", "term"),
                name="ix_enrollment_student_term",
            )
        ]


class BulkImportStatus(models.TextChoices):
    PARSING = "parsing", "Parsing"
    READY = "ready", "Ready"
    COMMITTED = "committed", "Committed"


class BulkImportBatch(TenantScopedModel):
    """One uploaded workbook. Nothing is written to `students` until commit."""

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="bulk_import_batches",
    )
    session = models.ForeignKey(
        Session, on_delete=models.PROTECT, related_name="+"
    )
    term = models.ForeignKey(Term, on_delete=models.PROTECT, related_name="+")
    status = models.CharField(
        max_length=10,
        choices=BulkImportStatus.choices,
        default=BulkImportStatus.PARSING,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bulk_import_batches"


class RowValidationStatus(models.TextChoices):
    VALID = "valid", "Valid"
    ERROR = "error", "Error"


class BulkImportRow(TenantScopedModel):
    """One staged spreadsheet row, validated before commit.

    sheet_name is the worksheet tab, which doubles as the class name — the
    tab-name validation (docs/02_Database.md) is the mechanism that keeps
    imports from inventing classes.
    """

    batch = models.ForeignKey(
        BulkImportBatch, on_delete=models.CASCADE, related_name="rows"
    )
    sheet_name = models.CharField(max_length=100)
    row_number = models.PositiveIntegerField()

    first_name = models.CharField(max_length=100, blank=True)
    middle_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    gender = models.CharField(max_length=10, choices=Gender.choices, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    father_name = models.CharField(max_length=100, blank=True)
    mother_name = models.CharField(max_length=100, blank=True)
    guardian_name = models.CharField(max_length=100, blank=True)
    guardian_phone = models.CharField(max_length=20, blank=True)
    guardian_email = models.EmailField(blank=True)
    address = models.TextField(blank=True)

    validation_status = models.CharField(
        max_length=10,
        choices=RowValidationStatus.choices,
        default=RowValidationStatus.VALID,
    )
    error_reason = models.TextField(blank=True)
    selected_for_commit = models.BooleanField(default=True)

    class Meta:
        db_table = "bulk_import_rows"
        ordering = ("row_number",)
