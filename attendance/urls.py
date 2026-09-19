"""
Attendance URL patterns.

All paths are nested under /attendance/ by the project-level include.
"""

from django.urls import path

from . import views

app_name = "attendance"

urlpatterns = [
    path("", views.AttendanceDashboardView.as_view(), name="dashboard"),
    path(
        "class/<int:class_id>/",
        views.ClassMonthlyRegisterView.as_view(),
        name="class_register",
    ),
    path(
        "class/<int:class_id>/mark-all-present/",
        views.MarkTodayAllPresentView.as_view(),
        name="mark_all_present",
    ),
    path(
        "class/<int:class_id>/unmark-all/",
        views.ClearDailyAttendanceView.as_view(),
        name="unmark_all",
    ),
    path(
        "class/<int:class_id>/save-daily/",
        views.SaveDailyRegisterView.as_view(),
        name="save_daily",
    ),
    path(
        "class/<int:class_id>/export/",
        views.ClassAttendanceExportView.as_view(),
        name="export_csv",
    ),
]
