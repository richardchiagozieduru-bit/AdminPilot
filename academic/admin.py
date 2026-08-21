from django.contrib import admin
from core.admin import TenantScopedModelAdmin
from .models import Class, Session, Term


@admin.register(Session)
class SessionAdmin(TenantScopedModelAdmin):
    list_display = ("name", "institution", "start_date", "end_date", "is_current")
    list_filter = ("is_current", "institution")
    search_fields = ("name",)


@admin.register(Term)
class TermAdmin(TenantScopedModelAdmin):
    list_display = ("name", "session", "institution", "start_date", "end_date", "is_current")
    list_filter = ("is_current", "institution")
    search_fields = ("name", "session__name")


@admin.register(Class)
class ClassAdmin(TenantScopedModelAdmin):
    list_display = ("name", "institution", "order", "status")
    list_filter = ("status", "institution")
    search_fields = ("name",)
