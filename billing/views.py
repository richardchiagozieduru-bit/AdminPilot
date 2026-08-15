"""
Billing views: fee structures, fee assignments/adjustments, payments,
receipts, and student credit.

Role gating follows docs/04_Permission_Matrix.md:
  - Fee Structure & Payment: Owner, Administrator, Bursar (full access)
  - Staff: no access

All mutating views write an AuditLog entry inside the same transaction as the
mutation, through the service layer (billing/services.py).
"""

from decimal import Decimal
import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError, models
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import DetailView, FormView, ListView, TemplateView, View

from academic.models import Class
from core.mixins import RoleRequiredMixin, TenantScopedQuerysetMixin
from core.services import write_audit_log

from .forms import (
    FeeStructureForm,
    FeeStructureItemFormSet,
    PaymentForm,
    PaymentReversalForm,
    StudentFeeAdjustmentForm,
)
from .models import (
    CreditTransaction,
    FeeStructure,
    Payment,
    PaymentItemAllocation,
    PaymentStatus,
    Receipt,
    StudentFeeAssignment,
)
from .services import (
    adjust_student_fee,
    create_fee_structure,
    delete_fee_structure,
    record_payment,
    reverse_payment,
    update_fee_structure,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
class BillingFormKwargsMixin:
    """Pass institution_id into billing forms, same pattern as StudentFormKwargsMixin."""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["institution_id"] = self.request.institution_id
        return kwargs


# --------------------------------------------------------------------------- #
# Fee Structures
# --------------------------------------------------------------------------- #
class FeeStructureListView(
    RoleRequiredMixin, TenantScopedQuerysetMixin, ListView
):
    """`/fee-structures/?term_id=` — filterable list."""

    model = FeeStructure
    template_name = "billing/fee_structure_list.html"
    context_object_name = "fee_structures"
    module = "fee_structures"
    paginate_by = 50

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .filter(is_active=True)
            .select_related("klass", "session", "term")
        )
        term_id = self.request.GET.get("term_id")
        if term_id:
            queryset = queryset.filter(term_id=term_id)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from academic.models import Term

        context["can_manage"] = self.can_manage()
        context["terms"] = Term.objects.all().order_by(
            "session__start_date", "start_date"
        )
        context["selected_term_id"] = self.request.GET.get("term_id", "")
        return context


class FeeStructureCreateView(RoleRequiredMixin, TemplateView):
    """`/fee-structures/add/` — form with inline formset for items.

    docs/08_UI_UX.md: "Add another line" submits/reloads with an extra blank row
    via the management form. Running total is recalculated on each page load.
    """

    template_name = "billing/fee_structure_form.html"
    module = "fee_structures"
    module_action = "manage"

    def get_form_and_formset(self, data=None):
        form = FeeStructureForm(
            data, institution_id=self.request.institution_id
        )
        formset = FeeStructureItemFormSet(data, prefix="items")
        return form, formset

    def get(self, request, *args, **kwargs):
        form, formset = self.get_form_and_formset()
        return self.render_to_response(
            self.get_context_data(form=form, formset=formset)
        )

    def post(self, request, *args, **kwargs):
        form, formset = self.get_form_and_formset(request.POST)

        if form.is_valid() and formset.is_valid():
            items = []
            for item_form in formset:
                if item_form.cleaned_data and not item_form.cleaned_data.get(
                    "DELETE", False
                ):
                    items.append(
                        {
                            "name": item_form.cleaned_data["name"],
                            "amount": item_form.cleaned_data["amount"],
                        }
                    )

            if not items:
                messages.error(request, "Add at least one fee item.")
                return self.render_to_response(
                    self.get_context_data(form=form, formset=formset)
                )

            try:
                structure = create_fee_structure(
                    institution_id=request.institution_id,
                    name=form.cleaned_data["name"],
                    klass=form.cleaned_data["klass"],
                    session=form.cleaned_data["session"],
                    term=form.cleaned_data["term"],
                    items=items,
                    actor=request.user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(
                    request,
                    f"Fee structure '{structure.name}' created successfully.",
                )
                return redirect("billing:fee_structure_detail", pk=structure.pk)
            except DatabaseError:
                logger.exception("Fee structure creation failed")
                messages.error(
                    request,
                    "Something went wrong creating the fee structure. Please try again.",
                )

        return self.render_to_response(
            self.get_context_data(form=form, formset=formset)
        )


class FeeStructureDetailView(
    RoleRequiredMixin, TenantScopedQuerysetMixin, DetailView
):
    """`/fee-structures/<id>/` — detail with items."""

    model = FeeStructure
    template_name = "billing/fee_structure_detail.html"
    context_object_name = "fee_structure"
    module = "fee_structures"

    def get_queryset(self):
        return super().get_queryset().select_related("klass", "session", "term")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["items"] = self.object.items.all()
        context["can_manage"] = self.can_manage()
        context["assignment_count"] = self.object.assignments.count()
        return context


class FeeStructureUpdateView(RoleRequiredMixin, TenantScopedQuerysetMixin, TemplateView):
    """`/fee-structures/<id>/edit/` — edit items. Rejected if locked.

    docs/08_UI_UX.md: if locked, renders in a read-only state with a banner
    pointing to individual student adjustments instead.
    """

    template_name = "billing/fee_structure_form.html"
    module = "fee_structures"
    module_action = "manage"

    def get_object(self):
        return get_object_or_404(
            FeeStructure.objects.filter(
                institution_id=self.request.institution_id
            ).select_related("klass", "session", "term"),
            pk=self.kwargs["pk"],
        )

    def get(self, request, *args, **kwargs):
        fee_structure = self.get_object()
        if fee_structure.locked:
            messages.warning(
                request,
                "This fee structure is locked because at least one payment has "
                "been recorded against it. To make a correction, adjust "
                "individual student assignments instead.",
            )
            return redirect("billing:fee_structure_detail", pk=fee_structure.pk)

        form = FeeStructureForm(
            instance=fee_structure,
            institution_id=request.institution_id,
        )
        formset = FeeStructureItemFormSet(
            instance=fee_structure, prefix="items"
        )
        return self.render_to_response(
            self.get_context_data(
                form=form, formset=formset, fee_structure=fee_structure
            )
        )

    def post(self, request, *args, **kwargs):
        fee_structure = self.get_object()
        if fee_structure.locked:
            messages.warning(
                request,
                "This fee structure is locked — edit rejected.",
            )
            return redirect("billing:fee_structure_detail", pk=fee_structure.pk)

        form = FeeStructureForm(
            request.POST,
            instance=fee_structure,
            institution_id=request.institution_id,
        )
        formset = FeeStructureItemFormSet(
            request.POST, instance=fee_structure, prefix="items"
        )

        if form.is_valid() and formset.is_valid():
            items = []
            for item_form in formset:
                if item_form.cleaned_data and not item_form.cleaned_data.get(
                    "DELETE", False
                ):
                    items.append(
                        {
                            "name": item_form.cleaned_data["name"],
                            "amount": item_form.cleaned_data["amount"],
                        }
                    )

            if not items:
                messages.error(request, "Add at least one fee item.")
                return self.render_to_response(
                    self.get_context_data(
                        form=form, formset=formset, fee_structure=fee_structure
                    )
                )

            try:
                update_fee_structure(
                    fee_structure=fee_structure,
                    name=form.cleaned_data["name"],
                    items=items,
                    actor=request.user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(request, "Fee structure updated.")
                return redirect(
                    "billing:fee_structure_detail", pk=fee_structure.pk
                )
            except ValidationError as e:
                messages.error(request, str(e.message))
            except DatabaseError:
                logger.exception("Fee structure update failed")
                messages.error(
                    request,
                    "Something went wrong updating the fee structure.",
                )

        return self.render_to_response(
            self.get_context_data(
                form=form, formset=formset, fee_structure=fee_structure
            )
        )


class FeeStructureDeleteView(
    RoleRequiredMixin, TenantScopedQuerysetMixin, TemplateView
):
    """`/fee-structures/<id>/delete/` — delete a fee structure (open or locked)."""

    template_name = "billing/fee_structure_confirm_delete.html"
    module = "fee_structures"
    module_action = "manage"

    def get_object(self):
        return get_object_or_404(
            FeeStructure.objects.filter(
                institution_id=self.request.institution_id
            ).select_related("klass", "session", "term"),
            pk=self.kwargs["pk"],
        )

    def get(self, request, *args, **kwargs):
        fee_structure = self.get_object()
        has_payments = fee_structure.assignments.filter(payments__isnull=False).exists()
        payment_count = Payment.objects.filter(assignment__fee_structure=fee_structure).count()

        return self.render_to_response(
            self.get_context_data(
                fee_structure=fee_structure,
                items=fee_structure.items.all(),
                assignment_count=fee_structure.assignments.count(),
                is_locked=fee_structure.locked or has_payments,
                payment_count=payment_count,
            )
        )

    def post(self, request, *args, **kwargs):
        fee_structure = self.get_object()
        name = fee_structure.name
        try:
            delete_fee_structure(
                fee_structure=fee_structure,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request, f"Fee structure '{name}' was successfully deleted."
            )
            return redirect("billing:fee_structure_list")
        except ValidationError as e:
            messages.error(request, str(e.message))
            return redirect("billing:fee_structure_detail", pk=fee_structure.pk)
        except DatabaseError:
            logger.exception("Fee structure deletion failed")
            messages.error(
                request, "Something went wrong deleting the fee structure."
            )
            return redirect("billing:fee_structure_detail", pk=fee_structure.pk)


# --------------------------------------------------------------------------- #
# Fee Assignments
# --------------------------------------------------------------------------- #
class FeeAssignmentListView(
    RoleRequiredMixin, TenantScopedQuerysetMixin, ListView
):
    """`/fee-structures/<id>/assignments/` — student fee adjustments list."""

    model = StudentFeeAssignment
    template_name = "billing/fee_assignment_list.html"
    context_object_name = "assignments"
    module = "fee_structures"
    paginate_by = 50

    def get_queryset(self):
        self.fee_structure = get_object_or_404(
            FeeStructure.objects.filter(
                institution_id=self.request.institution_id
            ),
            pk=self.kwargs["pk"],
        )
        return (
            StudentFeeAssignment.objects.filter(
                institution_id=self.request.institution_id,
                fee_structure=self.fee_structure,
            )
            .select_related("student")
            .order_by("student__last_name", "student__first_name")
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["fee_structure"] = self.fee_structure
        context["can_manage"] = self.can_manage()
        return context


class FeeAssignmentAdjustView(RoleRequiredMixin, FormView):
    """`/fee-assignments/<id>/adjust/` — adjust a student's fee, reason required."""

    template_name = "billing/fee_assignment_adjust.html"
    form_class = StudentFeeAdjustmentForm
    module = "fee_structures"
    module_action = "manage"

    def dispatch(self, request, *args, **kwargs):
        self.assignment = get_object_or_404(
            StudentFeeAssignment.objects.filter(
                institution_id=request.institution_id
            ).select_related("student", "fee_structure", "fee_structure__klass"),
            pk=kwargs["pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {"amount_due": self.assignment.amount_due}

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["assignment"] = self.assignment
        return context

    def form_valid(self, form):
        try:
            adjust_student_fee(
                assignment=self.assignment,
                new_amount=form.cleaned_data["amount_due"],
                reason=form.cleaned_data["adjustment_reason"],
                actor=self.request.user,
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                self.request,
                f"Fee adjusted for {self.assignment.student.full_name}.",
            )
            return redirect(
                "billing:fee_assignment_list",
                pk=self.assignment.fee_structure_id,
            )
        except ValidationError as e:
            messages.error(self.request, str(e.message))
            return self.form_invalid(form)
        except DatabaseError:
            logger.exception("Fee adjustment failed")
            messages.error(
                self.request,
                "Something went wrong saving the adjustment.",
            )
            return self.form_invalid(form)


# --------------------------------------------------------------------------- #
# Payments
# --------------------------------------------------------------------------- #
class PaymentListView(
    RoleRequiredMixin, TenantScopedQuerysetMixin, ListView
):
    """`/payments/` — filterable payment list."""

    model = Payment
    template_name = "billing/payment_list.html"
    context_object_name = "payments"
    module = "payments"
    paginate_by = 50

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .select_related(
                "assignment__student",
                "assignment__fee_structure",
                "assignment__fee_structure__klass",
                "recorded_by",
                "receipt",
            )
        )

        date_from = self.request.GET.get("date_from")
        date_to = self.request.GET.get("date_to")
        method = self.request.GET.get("method")
        status = self.request.GET.get("status")

        if date_from:
            queryset = queryset.filter(payment_date__gte=date_from)
        if date_to:
            queryset = queryset.filter(payment_date__lte=date_to)
        if method:
            queryset = queryset.filter(method=method)
        if status:
            queryset = queryset.filter(status=status)

        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_manage"] = self.can_manage()
        context["method_choices"] = Payment._meta.get_field("method").choices
        context["status_choices"] = Payment._meta.get_field("status").choices
        context["selected_date_from"] = self.request.GET.get("date_from", "")
        context["selected_date_to"] = self.request.GET.get("date_to", "")
        context["selected_method"] = self.request.GET.get("method", "")
        context["selected_status"] = self.request.GET.get("status", "")
        return context


class PaymentCreateView(RoleRequiredMixin, BillingFormKwargsMixin, FormView):
    """`/payments/add/` — record a payment.

    docs/03_Views_and_Endpoints.md: POST wraps payment insert + receipt-counter
    increment + receipt insert + credit in one transaction.atomic().
    """

    template_name = "billing/payment_form.html"
    form_class = PaymentForm
    module = "payments"
    module_action = "manage"

    def get_initial(self):
        initial = super().get_initial()
        assignment_id = self.request.GET.get("assignment_id")
        student_id = self.request.GET.get("student_id")
        if assignment_id:
            try:
                assignment = StudentFeeAssignment.objects.get(
                    pk=assignment_id,
                    institution_id=self.request.institution_id,
                )
                initial["assignment"] = assignment
            except StudentFeeAssignment.DoesNotExist:
                pass
        elif student_id:
            assignment = (
                StudentFeeAssignment.objects.filter(
                    student_id=student_id,
                    institution_id=self.request.institution_id,
                )
                .order_by("-created_at")
                .first()
            )
            if assignment:
                initial["assignment"] = assignment
        return initial

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        import json

        # 1. Fetch active classes
        classes = Class.objects.filter(
            institution_id=self.request.institution_id,
            status="active",
        ).order_by("order", "name")
        context["classes"] = classes

        # 2. Bulk fetch allocations grouped by (assignment_id, fee_item_id) in 1 query
        allocations_qs = (
            PaymentItemAllocation.objects.filter(
                payment__assignment__institution_id=self.request.institution_id,
                payment__status=PaymentStatus.ACTIVE,
            )
            .order_by()
            .values("payment__assignment_id", "fee_item_id")
            .annotate(total_paid=models.Sum("amount"))
        )
        alloc_map = {}
        for r in allocations_qs:
            aid = r["payment__assignment_id"]
            fid = r["fee_item_id"]
            if aid not in alloc_map:
                alloc_map[aid] = {}
            alloc_map[aid][fid] = r["total_paid"]

        # 3. Bulk fetch active payments sum & credit transactions per assignment in 1 query
        payments_qs = (
            Payment.objects.filter(
                assignment__institution_id=self.request.institution_id,
                status=PaymentStatus.ACTIVE,
            )
            .order_by()
            .values("assignment_id")
            .annotate(
                total_paid=models.Sum("amount"),
                credited_back=models.Sum("credit_transactions__amount"),
            )
        )
        payments_map = {
            r["assignment_id"]: (r["total_paid"] or Decimal("0.00")) - (r["credited_back"] or Decimal("0.00"))
            for r in payments_qs
        }

        # 4. Bulk fetch applied credits per assignment in 1 query
        credits_qs = (
            CreditTransaction.objects.filter(
                applied_to_assignment__institution_id=self.request.institution_id,
            )
            .order_by()
            .values("applied_to_assignment_id")
            .annotate(total_credit=models.Sum("amount"))
        )
        credits_map = {
            r["applied_to_assignment_id"]: abs(r["total_credit"] or Decimal("0.00"))
            for r in credits_qs
        }

        # 5. Fetch all assignments with student, fee structure, and prefetched items
        assignments = (
            StudentFeeAssignment.objects.filter(
                institution_id=self.request.institution_id
            )
            .select_related("student", "fee_structure", "fee_structure__klass")
            .prefetch_related("fee_structure__items")
        )

        data = {}
        for a in assignments:
            paid = payments_map.get(a.pk, Decimal("0.00"))
            applied_credits = credits_map.get(a.pk, Decimal("0.00"))
            outstanding = a.amount_due - (paid - applied_credits)

            if outstanding > 0:
                paid_item_map = alloc_map.get(a.pk, {})
                total_allocated = sum(paid_item_map.values(), Decimal("0.00"))
                total_active_paid = paid + applied_credits
                unallocated_paid = max(Decimal("0.00"), total_active_paid - total_allocated)
                remaining_unallocated = unallocated_paid

                items_breakdown = []
                for item in a.fee_structure.items.all():
                    direct_paid = paid_item_map.get(item.pk, Decimal("0.00"))
                    billed = item.amount
                    fallback_applied = Decimal("0.00")
                    if remaining_unallocated > 0:
                        needed = max(Decimal("0.00"), billed - direct_paid)
                        fallback_applied = min(needed, remaining_unallocated)
                        remaining_unallocated -= fallback_applied
                    total_item_paid = direct_paid + fallback_applied
                    remaining = max(Decimal("0.00"), billed - total_item_paid)

                    if total_item_paid >= billed and billed > 0:
                        status = "paid"
                    elif total_item_paid > 0:
                        status = "partial"
                    else:
                        status = "unpaid"

                    items_breakdown.append({
                        "fee_item_id": item.pk,
                        "name": item.name,
                        "billed": str(billed),
                        "paid": str(total_item_paid),
                        "remaining": str(remaining),
                        "status": status,
                    })

                data[str(a.pk)] = {
                    "student_id": str(a.student_id),
                    "student_name": a.student.full_name,
                    "admission_number": a.student.admission_number,
                    "class_id": str(a.fee_structure.klass_id),
                    "class_name": a.fee_structure.klass.name,
                    "fee_structure_id": str(a.fee_structure_id),
                    "fee_structure_name": a.fee_structure.name,
                    "total_billed": str(a.amount_due),
                    "total_outstanding": str(outstanding),
                    "items": items_breakdown,
                }
        context["assignments_json"] = json.dumps(data)
        return context

    def form_valid(self, form):
        try:
            payment, receipt, credit_applied, credit_created = record_payment(
                assignment=form.cleaned_data["assignment"],
                amount=form.cleaned_data["amount"],
                payment_date=form.cleaned_data["payment_date"],
                method=form.cleaned_data["method"],
                actor=self.request.user,
                apply_credit=form.cleaned_data.get("apply_credit", False),
                item_allocations=form.cleaned_data.get("cleaned_allocations"),
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
            msg = f"Payment of {payment.amount} recorded — receipt {receipt.receipt_number}."
            if credit_applied > 0:
                msg += f" {credit_applied} credit was applied."
            if credit_created > 0:
                msg += f" {credit_created} added to student's credit balance."
            messages.success(self.request, msg)
            return redirect("billing:receipt_detail", pk=receipt.pk)
        except ValidationError as e:
            messages.error(self.request, str(e.message))
            return self.form_invalid(form)
        except DatabaseError:
            logger.exception("Payment recording failed")
            messages.error(
                self.request,
                "Something went wrong recording the payment. Please try again.",
            )
            return self.form_invalid(form)


class PaymentDetailView(
    RoleRequiredMixin, TenantScopedQuerysetMixin, DetailView
):
    """`/payments/<id>/` — payment detail."""

    model = Payment
    template_name = "billing/payment_detail.html"
    context_object_name = "payment"
    module = "payments"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "assignment__student",
                "assignment__fee_structure",
                "assignment__fee_structure__klass",
                "recorded_by",
                "receipt",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_manage"] = self.can_manage()
        context["student"] = self.object.assignment.student
        return context


class PaymentReverseView(RoleRequiredMixin, FormView):
    """`/payments/<id>/reverse/` — reverse a payment, reason required."""

    template_name = "billing/payment_reverse.html"
    form_class = PaymentReversalForm
    module = "payments"
    module_action = "manage"

    def dispatch(self, request, *args, **kwargs):
        self.payment = get_object_or_404(
            Payment.objects.filter(
                institution_id=request.institution_id
            ).select_related(
                "assignment__student",
                "assignment__fee_structure",
                "receipt",
            ),
            pk=kwargs["pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["payment"] = self.payment
        context["student"] = self.payment.assignment.student
        return context

    def form_valid(self, form):
        try:
            reverse_payment(
                payment=self.payment,
                reason=form.cleaned_data["reversal_reason"],
                actor=self.request.user,
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
            messages.success(self.request, "Payment reversed.")
            return redirect("billing:payment_detail", pk=self.payment.pk)
        except ValidationError as e:
            messages.error(self.request, str(e.message))
            return self.form_invalid(form)
        except DatabaseError:
            logger.exception("Payment reversal failed")
            messages.error(
                self.request,
                "Something went wrong reversing the payment.",
            )
            return self.form_invalid(form)


# --------------------------------------------------------------------------- #
# Student Credit
# --------------------------------------------------------------------------- #
class StudentCreditView(RoleRequiredMixin, TemplateView):
    """`/students/<id>/credit/` — credit balance and transaction history."""

    template_name = "billing/student_credit.html"
    module = "payments"

    def get_context_data(self, **kwargs):
        from django.db.models import Q

        from students.models import Student

        context = super().get_context_data(**kwargs)
        student = get_object_or_404(
            Student.objects.filter(institution_id=self.request.institution_id),
            pk=self.kwargs["pk"],
        )
        context["student"] = student

        # Credit transactions link to a student through two paths:
        # positive (overpayment) → source_payment.assignment.student
        # negative (applied)    → applied_to_assignment.student
        from .models import CreditTransaction

        context["transactions"] = (
            CreditTransaction.unscoped.filter(
                Q(source_payment__assignment__student=student)
                | Q(applied_to_assignment__student=student)
            )
            .select_related("source_payment", "applied_to_assignment")
            .order_by("-created_at")
        )
        return context


# --------------------------------------------------------------------------- #
# Receipts
# --------------------------------------------------------------------------- #
class ReceiptDetailView(
    RoleRequiredMixin, TenantScopedQuerysetMixin, DetailView
):
    """`/receipts/<id>/` — receipt view, styled for browser print.

    docs/08_UI_UX.md: uses a separate print-friendly shell, a Print button
    calling window.print(), and no PDF generation.
    """

    model = Receipt
    template_name = "billing/receipt_detail.html"
    context_object_name = "receipt"
    module = "receipts"

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related(
                "payment__assignment__student",
                "payment__assignment__fee_structure",
                "payment__assignment__fee_structure__klass",
                "issued_by",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        payment = self.object.payment
        context["payment"] = payment
        context["student"] = payment.assignment.student
        context["fee_structure"] = payment.assignment.fee_structure
        context["institution"] = payment.institution
        context["allocations"] = payment.allocations.select_related("fee_item")
        context["assignment"] = payment.assignment
        context["remaining_package_balance"] = payment.assignment.outstanding_balance
        return context
