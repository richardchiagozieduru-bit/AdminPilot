"""
Super Admin URLs, mounted at /platform/.

Its own namespace and its own URLconf (docs/03_Views_and_Endpoints.md, CR-004):
no institution-scoped URL can resolve through this file, and no platform URL
can resolve through accounts/urls.py.
"""

from django.urls import path

from . import views

app_name = "platform_admin"

urlpatterns = [
    path("login/", views.PlatformLoginView.as_view(), name="login"),
    path("logout/", views.PlatformLogoutView.as_view(), name="logout"),
    path(
        "institutions/",
        views.PendingInstitutionsListView.as_view(),
        name="institution_list",
    ),
    path(
        "institutions/<int:pk>/approve/",
        views.ApproveInstitutionView.as_view(),
        name="approve_institution",
    ),
    path(
        "institutions/<int:pk>/reject/",
        views.RejectInstitutionView.as_view(),
        name="reject_institution",
    ),
]
