"""
Student and bulk-import URLs.

Mounted at the root (AdminPilot/urls.py), so the paths written here are the ones
in docs/03_Views_and_Endpoints.md verbatim: /students/, /students/add/,
/students/<id>/..., and the /students/import/ flow.

The four profile tabs (overview/enrollments/payments/timeline) are sibling URLs
rather than one view switching on a query string, so each tab is linkable and
back/forward works the way a school expects. docs/03 names only /students/<id>/;
the tab routes are the natural extension of the tabbed profile it describes.

The bulk-import routes are grouped before the `<int:pk>` routes for reading
order. The converters already make them unambiguous — "import" and "add" are not
integers, so they can never be captured by `<int:pk>` — but keeping the flow
together beats relying on that to notice a clash.
"""

from django.urls import path

from . import imports, views

app_name = "students"

urlpatterns = [
    path("students/", views.StudentListView.as_view(), name="list"),
    path("students/add/", views.StudentCreateView.as_view(), name="add"),
    # Bulk import: template download → upload → preview/commit.
    path("students/import/", imports.BulkImportUploadView.as_view(), name="import"),
    path(
        "students/import/template/",
        imports.BulkImportTemplateView.as_view(),
        name="import_template",
    ),
    path(
        "students/import/<int:batch_id>/preview/",
        imports.BulkImportPreviewView.as_view(),
        name="import_preview",
    ),
    # Profile tabs — one view each, all rendering student_detail.html.
    path("students/<int:pk>/", views.StudentDetailView.as_view(), name="detail"),
    path(
        "students/<int:pk>/enrollments/",
        views.StudentEnrollmentsView.as_view(),
        name="enrollments",
    ),
    path(
        "students/<int:pk>/payments/",
        views.StudentPaymentsView.as_view(),
        name="payments",
    ),
    path(
        "students/<int:pk>/timeline/",
        views.StudentTimelineView.as_view(),
        name="timeline",
    ),
    path("students/<int:pk>/edit/", views.StudentUpdateView.as_view(), name="edit"),
    # POST-only state changes — see StudentStatusChangeView.
    path(
        "students/<int:pk>/archive/",
        views.StudentArchiveView.as_view(),
        name="archive",
    ),
    path(
        "students/<int:pk>/reactivate/",
        views.StudentReactivateView.as_view(),
        name="reactivate",
    ),
    path(
        "students/<int:pk>/delete/",
        views.StudentDeleteView.as_view(),
        name="delete",
    ),
]
