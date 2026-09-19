"""
Attendance models: per-class daily registers and individual student records.

ClassAttendanceRegister is one class on one day — it holds the metadata (who
took attendance, cached counts) and owns the child AttendanceRecord rows, one
per enrolled student. The two together form the attendance matrix that the
Unified Monthly Sheet view renders.

Constraints:
  - uniq_attendance_class_date: One register per class per calendar date.
  - uniq_register_student: One record per student per register.
"""

from django.conf import settings
from django.db import models

from core.models import TenantScopedModel


class AttendanceStatus(models.TextChoices):
    PRESENT = "present", "Present"
    ABSENT = "absent", "Absent"
    LATE = "late", "Late"
    EXCUSED = "excused", "Excused"


class ClassAttendanceRegister(TenantScopedModel):
    """One class, one day. Created when roll call is first taken."""

    klass = models.ForeignKey(
        "academic.Class",
        on_delete=models.PROTECT,
        related_name="attendance_registers",
    )
    session = models.ForeignKey(
        "academic.Session",
        on_delete=models.PROTECT,
        related_name="attendance_registers",
    )
    term = models.ForeignKey(
        "academic.Term",
        on_delete=models.PROTECT,
        related_name="attendance_registers",
    )
    date = models.DateField(
        help_text="The calendar date this register covers.",
    )
    taken_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="registers_taken",
    )

    # Cached counts — updated atomically when records are saved so the
    # dashboard never needs to COUNT(*) across child rows.
    total_students = models.PositiveIntegerField(default=0)
    present_count = models.PositiveIntegerField(default=0)
    absent_count = models.PositiveIntegerField(default=0)
    late_count = models.PositiveIntegerField(default=0)
    excused_count = models.PositiveIntegerField(default=0)

    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "class_attendance_registers"
        ordering = ("-date",)
        constraints = [
            models.UniqueConstraint(
                fields=("institution", "klass", "date"),
                name="uniq_attendance_class_date",
            )
        ]

    def __str__(self):
        return f"{self.klass} — {self.date}"

    @property
    def attendance_rate(self):
        """Percentage of students present (including late) for this day."""
        if self.total_students == 0:
            return 0
        return round(
            (self.present_count + self.late_count) / self.total_students * 100, 1
        )

    def refresh_counts(self):
        """Recalculate cached counts from child records."""
        records = self.records.all()
        self.total_students = records.count()
        self.present_count = records.filter(status=AttendanceStatus.PRESENT).count()
        self.absent_count = records.filter(status=AttendanceStatus.ABSENT).count()
        self.late_count = records.filter(status=AttendanceStatus.LATE).count()
        self.excused_count = records.filter(status=AttendanceStatus.EXCUSED).count()
        self.save(
            update_fields=[
                "total_students",
                "present_count",
                "absent_count",
                "late_count",
                "excused_count",
                "updated_at",
            ]
        )


class AttendanceRecord(TenantScopedModel):
    """One student's status for one day."""

    register = models.ForeignKey(
        ClassAttendanceRegister,
        on_delete=models.CASCADE,
        related_name="records",
    )
    student = models.ForeignKey(
        "students.Student",
        on_delete=models.PROTECT,
        related_name="attendance_records",
    )
    status = models.CharField(
        max_length=8,
        choices=AttendanceStatus.choices,
        default=AttendanceStatus.PRESENT,
    )
    remark = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(TenantScopedModel.Meta):
        db_table = "attendance_records"
        ordering = ("student__last_name", "student__first_name")
        constraints = [
            models.UniqueConstraint(
                fields=("register", "student"),
                name="uniq_register_student",
            )
        ]

    def __str__(self):
        return f"{self.student} — {self.get_status_display()}"
