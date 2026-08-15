"""
Reports and Data Export URLs.

Mounted at root in AdminPilot/urls.py:
  - /reports/
  - /export/
"""

from django.urls import path

from . import views

app_name = "reports"

urlpatterns = [
    # Reports Hub
    path("reports/", views.ReportsHubView.as_view(), name="hub"),
    # Income Report
    path("reports/income/", views.IncomeReportView.as_view(), name="income"),
    path(
        "reports/income/export/",
        views.IncomeReportExportView.as_view(),
        name="income_export",
    ),
    # Outstanding Fees Report
    path(
        "reports/outstanding-fees/",
        views.OutstandingFeesReportView.as_view(),
        name="outstanding_fees",
    ),
    path(
        "reports/outstanding-fees/export/",
        views.OutstandingFeesExportView.as_view(),
        name="outstanding_fees_export",
    ),
    # Student Payment History Report
    path(
        "reports/student/<int:pk>/payment-history/",
        views.StudentPaymentHistoryReportView.as_view(),
        name="student_payment_history",
    ),
    path(
        "reports/student/<int:pk>/payment-history/export/",
        views.StudentPaymentHistoryExportView.as_view(),
        name="student_payment_history_export",
    ),
    # Class Summary Report
    path(
        "reports/class-summary/",
        views.ClassSummaryReportView.as_view(),
        name="class_summary",
    ),
    path(
        "reports/class-summary/export/",
        views.ClassSummaryExportView.as_view(),
        name="class_summary_export",
    ),
    # Data Export (Owner only)
    path("export/students/", views.StudentExportView.as_view(), name="export_students"),
    path("export/payments/", views.PaymentExportView.as_view(), name="export_payments"),
]
