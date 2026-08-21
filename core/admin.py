from django.contrib import admin
from core.middleware import institution_db_context
from .models import AuditLog, Institution


class TenantScopedModelAdmin(admin.ModelAdmin):
    """ModelAdmin base that handles SQL Server RLS context and unscoped querysets."""

    def get_queryset(self, request):
        if hasattr(self.model, "unscoped"):
            return self.model.unscoped.all()
        return super().get_queryset(request)

    def save_model(self, request, obj, form, change):
        if getattr(obj, "institution_id", None):
            with institution_db_context(obj.institution_id):
                super().save_model(request, obj, form, change)
        else:
            super().save_model(request, obj, form, change)

    def delete_model(self, request, obj):
        if getattr(obj, "institution_id", None):
            with institution_db_context(obj.institution_id):
                super().delete_model(request, obj)
        else:
            super().delete_model(request, obj)


@admin.register(Institution)
class InstitutionAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "type", "status", "email", "phone", "created_at")
    list_filter = ("status", "type")
    search_fields = ("name", "code", "email")
    readonly_fields = ("created_at", "updated_at", "reviewed_at")


@admin.register(AuditLog)
class AuditLogAdmin(TenantScopedModelAdmin):
    list_display = ("created_at", "institution", "actor", "action", "target_type", "target_id", "summary")
    list_filter = ("action", "target_type", "institution")
    search_fields = ("target_type", "target_id", "action", "summary")
    readonly_fields = ("created_at",)
