"""
Core institution URLs: the dashboard, the first-run wizard, institution settings
and the audit log.

Mounted at the root, last of the three includes, because `""` is the dashboard —
a pattern that would swallow anything mounted after it.
"""

from django.urls import path

from . import views

app_name = "core"

urlpatterns = [
    path("", views.DashboardView.as_view(), name="dashboard"),
    path("search/", views.SearchView.as_view(), name="search"),
    path("search/suggest/", views.SearchSuggestView.as_view(), name="search_suggest"),
    path("setup/", views.InstitutionSetupWizardView.as_view(), name="setup"),
    path(
        "settings/institution/",
        views.InstitutionSettingsView.as_view(),
        name="institution_settings",
    ),
    path("settings/audit-log/", views.AuditLogView.as_view(), name="audit_log"),
]
