"""
Academic structure URLs.

Mounted at the root, so the paths here are the ones in
docs/03_Views_and_Endpoints.md verbatim: /classes/... and /settings/academic/.
The two live in one app because a Class is only meaningful inside a Session and
Term, and splitting them would put one half of the same object graph in each of
two URLconfs.
"""

from django.urls import path

from . import views

app_name = "academic"

urlpatterns = [
    path("classes/", views.ClassListView.as_view(), name="class_list"),
    path("classes/add/", views.ClassCreateView.as_view(), name="class_add"),
    path("classes/<int:pk>/edit/", views.ClassUpdateView.as_view(), name="class_edit"),
    # POST only — see ClassStatusChangeView.
    path(
        "classes/<int:pk>/deactivate/",
        views.ClassDeactivateView.as_view(),
        name="class_deactivate",
    ),
    path(
        "classes/<int:pk>/reactivate/",
        views.ClassReactivateView.as_view(),
        name="class_reactivate",
    ),
    path(
        "settings/academic/",
        views.AcademicStructureView.as_view(),
        name="structure",
    ),
]
