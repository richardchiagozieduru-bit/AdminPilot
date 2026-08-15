"""
Root URL configuration.

Two top-level namespaces, kept apart on purpose (docs/03_Views_and_Endpoints.md):
  /platform/ — Super Admin only, no institution context ever set
  everything else — institution-scoped application

django.contrib.admin is not installed: it bypasses the role matrix and the
tenant middleware, so a logged-in staff user could read across institutions
through it. Every administrative action has a first-class view instead.

App URLconfs land with their views in Phases 2-9; the roadmap's phase order is
the order these get uncommented.
"""

from django.urls import include, path

urlpatterns = [
    path("platform/", include("platform_admin.urls")),
    path("", include("accounts.urls")),
    path("", include("academic.urls")),
    path("", include("billing.urls")),
    path("", include("students.urls")),
    path("", include("reports.urls")),
    # Last: core owns "" itself (the dashboard), and a pattern that matches the
    # empty path would shadow every include placed after it.
    path("", include("core.urls")),
]
