from django.contrib import admin
from .models import PlatformUser


@admin.register(PlatformUser)
class PlatformUserAdmin(admin.ModelAdmin):
    list_display = ("email", "full_name", "is_active", "created_at")
    list_filter = ("is_active",)
    search_fields = ("email", "full_name")
    readonly_fields = ("created_at", "updated_at")
