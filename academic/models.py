"""
Academic structure: Sessions, Terms, Classes.

The first-run wizard (docs/03_Views_and_Endpoints.md, /setup/) creates the
first Session/Term and initial Classes; ongoing creation happens under
/settings/academic/. Everything here is tenant-scoped — a Class belongs to
exactly one Institution, and the RLS predicate keeps it there.

Term and Session exist separately because a school's Session spans multiple
Terms, and enrollments (students/models.py) reference both.
"""

from django.db import models

from core.models import Institution, TenantScopedModel


class Session(TenantScopedModel):
    name = models.CharField(max_length=100)
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "sessions"
        ordering = ("-start_date",)

    def __str__(self):
        return f"{self.name} ({self.start_date} — {self.end_date})"


class Term(TenantScopedModel):
    session = models.ForeignKey(
        Session, on_delete=models.PROTECT, related_name="terms"
    )
    name = models.CharField(max_length=50)  # e.g. "First Term"
    start_date = models.DateField()
    end_date = models.DateField()
    is_current = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "terms"
        ordering = ("session__start_date", "start_date")

    def __str__(self):
        return f"{self.session.name} — {self.name}"


class ClassStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    INACTIVE = "inactive", "Inactive"


class Class(TenantScopedModel):
    # related_name is overridden from TenantScopedModel's %(class)ss pattern:
    # "class" + "s" would produce the awkward "classs".
    institution = models.ForeignKey(
        Institution, on_delete=models.PROTECT, related_name="classes"
    )
    name = models.CharField(max_length=100)
    order = models.PositiveIntegerField(default=0)
    status = models.CharField(
        max_length=8,
        choices=ClassStatus.choices,
        default=ClassStatus.ACTIVE,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "classes"
        ordering = ("order", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("institution", "name"),
                name="uniq_class_name_per_inst",
            )
        ]

    def __str__(self):
        return self.name
