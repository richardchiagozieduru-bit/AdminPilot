from django.contrib import admin
from core.admin import TenantScopedModelAdmin
from .models import BulkImportBatch, BulkImportRow, Student, StudentEnrollment


@admin.register(Student)
class StudentAdmin(TenantScopedModelAdmin):
    list_display = (
        "admission_number",
        "first_name",
        "last_name",
        "institution",
        "gender",
        "guardian_name",
        "guardian_phone",
        "status",
    )
    list_filter = ("status", "gender", "institution")
    search_fields = ("admission_number", "first_name", "last_name", "guardian_name", "guardian_phone")


@admin.register(StudentEnrollment)
class StudentEnrollmentAdmin(TenantScopedModelAdmin):
    list_display = ("student", "session", "term", "klass", "institution", "enrolled_at")
    list_filter = ("institution", "session", "term")
    search_fields = ("student__admission_number", "student__first_name", "student__last_name")


@admin.register(BulkImportBatch)
class BulkImportBatchAdmin(TenantScopedModelAdmin):
    list_display = ("id", "institution", "uploaded_by", "session", "term", "status", "created_at")
    list_filter = ("status", "institution")


@admin.register(BulkImportRow)
class BulkImportRowAdmin(TenantScopedModelAdmin):
    list_display = ("batch", "row_number", "sheet_name", "first_name", "last_name", "validation_status", "institution")
    list_filter = ("validation_status", "institution")
    search_fields = ("first_name", "last_name", "sheet_name")
