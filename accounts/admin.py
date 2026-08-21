from django import forms
from django.contrib import admin, messages
from django.contrib.auth.forms import SetPasswordForm
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

from core.middleware import institution_db_context

from .models import User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = (
        "email",
        "full_name",
        "institution",
        "role",
        "is_active",
        "created_at",
        "admin_actions",
    )
    list_filter = ("role", "is_active", "institution")
    search_fields = ("email", "full_name", "institution__name")
    readonly_fields = ("created_at", "updated_at")
    fields = (
        "email",
        "full_name",
        "phone",
        "institution",
        "role",
        "is_active",
        "created_at",
        "updated_at",
    )

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

    def admin_actions(self, obj):
        url = reverse("admin:accounts_user_password", args=[obj.pk])
        return format_html('<a class="button" href="{}">Reset Password</a>', url)

    admin_actions.short_description = "Password"

    def get_urls(self):
        urls = super().get_urls()
        custom_urls = [
            path(
                "<int:user_id>/password/",
                self.admin_site.admin_view(self.user_change_password),
                name="accounts_user_password",
            ),
        ]
        return custom_urls + urls

    def user_change_password(self, request, user_id):
        user = self.get_object(request, user_id)
        if user is None:
            messages.error(request, "User not found.")
            return redirect("admin:accounts_user_changelist")

        if request.method == "POST":
            form = SetPasswordForm(user, request.POST)
            if form.is_valid():
                with institution_db_context(user.institution_id):
                    form.save()
                messages.success(
                    request, f"Password for {user.email} changed successfully."
                )
                return redirect("admin:accounts_user_changelist")
        else:
            form = SetPasswordForm(user)

        context = {
            **self.admin_site.each_context(request),
            "title": f"Reset password: {user.email}",
            "form": form,
            "user_obj": user,
            "opts": self.model._meta,
        }
        return render(request, "admin/accounts/user_change_password.html", context)
