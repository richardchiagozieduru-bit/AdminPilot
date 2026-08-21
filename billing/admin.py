from django.contrib import admin
from core.admin import TenantScopedModelAdmin
from .models import (
    CreditTransaction,
    FeeStructure,
    FeeStructureItem,
    Payment,
    PaymentItemAllocation,
    Receipt,
    StudentFeeAssignment,
)


@admin.register(FeeStructure)
class FeeStructureAdmin(TenantScopedModelAdmin):
    list_display = ("name", "klass", "session", "term", "total_amount", "locked", "is_active", "institution")
    list_filter = ("locked", "is_active", "institution")
    search_fields = ("name", "klass__name")


@admin.register(FeeStructureItem)
class FeeStructureItemAdmin(TenantScopedModelAdmin):
    list_display = ("name", "fee_structure", "amount", "institution")
    list_filter = ("institution",)
    search_fields = ("name", "fee_structure__name")


@admin.register(StudentFeeAssignment)
class StudentFeeAssignmentAdmin(TenantScopedModelAdmin):
    list_display = ("student", "fee_structure", "amount_due", "institution", "created_at")
    list_filter = ("institution",)
    search_fields = ("student__admission_number", "student__first_name", "student__last_name")


@admin.register(Payment)
class PaymentAdmin(TenantScopedModelAdmin):
    list_display = ("id", "assignment", "amount", "payment_date", "method", "status", "institution", "recorded_by", "created_at")
    list_filter = ("method", "status", "institution")
    search_fields = ("assignment__student__admission_number", "assignment__student__first_name")


@admin.register(PaymentItemAllocation)
class PaymentItemAllocationAdmin(TenantScopedModelAdmin):
    list_display = ("id", "payment", "fee_item", "amount", "institution", "created_at")
    list_filter = ("institution",)


@admin.register(Receipt)
class ReceiptAdmin(TenantScopedModelAdmin):
    list_display = ("receipt_number", "payment", "institution", "issued_by", "created_at")
    list_filter = ("institution",)
    search_fields = ("receipt_number",)


@admin.register(CreditTransaction)
class CreditTransactionAdmin(TenantScopedModelAdmin):
    list_display = ("id", "amount", "source_payment", "applied_to_assignment", "institution", "created_at")
    list_filter = ("institution",)
