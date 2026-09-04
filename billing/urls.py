"""
Billing URLs.

Mounted at the root (AdminPilot/urls.py), so the paths here match
docs/03_Views_and_Endpoints.md verbatim: /fee-structures/, /payments/,
/receipts/, and the student-scoped credit view.
"""

from django.urls import path

from . import views

app_name = "billing"

urlpatterns = [
    # Fee Structures
    path(
        "fee-structures/",
        views.FeeStructureListView.as_view(),
        name="fee_structure_list",
    ),
    path(
        "fee-structures/add/",
        views.FeeStructureCreateView.as_view(),
        name="fee_structure_create",
    ),
    path(
        "fee-structures/<int:pk>/",
        views.FeeStructureDetailView.as_view(),
        name="fee_structure_detail",
    ),
    path(
        "fee-structures/<int:pk>/edit/",
        views.FeeStructureUpdateView.as_view(),
        name="fee_structure_edit",
    ),
    path(
        "fee-structures/<int:pk>/delete/",
        views.FeeStructureDeleteView.as_view(),
        name="fee_structure_delete",
    ),
    path(
        "fee-structures/<int:pk>/toggle-lock/",
        views.FeeStructureToggleLockView.as_view(),
        name="fee_structure_toggle_lock",
    ),
    path(
        "fee-structures/<int:pk>/assignments/",
        views.FeeAssignmentListView.as_view(),
        name="fee_assignment_list",
    ),
    path(
        "fee-structures/<int:pk>/sync-assignments/",
        views.FeeStructureSyncAssignmentsView.as_view(),
        name="fee_structure_sync_assignments",
    ),
    # Fee Assignments & Individual Student Packages
    path(
        "fee-assignments/<int:pk>/adjust/",
        views.FeeAssignmentAdjustView.as_view(),
        name="fee_assignment_adjust",
    ),
    path(
        "fee-assignments/<int:pk>/customize/",
        views.StudentFeePackageCustomizeView.as_view(),
        name="fee_assignment_customize",
    ),
    path(
        "students/<int:student_id>/fee-package/create/",
        views.StudentFeePackageCustomizeView.as_view(),
        name="student_fee_package_create",
    ),
    path(
        "fee-assignments/<int:pk>/apply-credit/",
        views.StudentCreditApplyView.as_view(),
        name="fee_assignment_apply_credit",
    ),
    # Payments
    path(
        "payments/",
        views.PaymentListView.as_view(),
        name="payment_list",
    ),
    path(
        "payments/export/",
        views.PaymentExportCSVView.as_view(),
        name="payment_export_csv",
    ),
    path(
        "payments/add/",
        views.PaymentCreateView.as_view(),
        name="payment_create",
    ),
    path(
        "payments/<int:pk>/",
        views.PaymentDetailView.as_view(),
        name="payment_detail",
    ),
    path(
        "payments/<int:pk>/reverse/",
        views.PaymentReverseView.as_view(),
        name="payment_reverse",
    ),
    # Student Credit
    path(
        "students/<int:pk>/credit/",
        views.StudentCreditView.as_view(),
        name="student_credit",
    ),
    path(
        "students/<int:pk>/credit/apply/",
        views.StudentCreditApplyView.as_view(),
        name="student_credit_apply",
    ),
    # Receipts
    path(
        "receipts/<int:pk>/",
        views.ReceiptDetailView.as_view(),
        name="receipt_detail",
    ),
]

