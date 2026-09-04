"""
Billing views: fee structures, fee assignments/adjustments, payments,
receipts, and student credit.

Role gating follows docs/04_Permission_Matrix.md:
  - Fee Structure & Payment: Owner, Administrator, Bursar (full access)
  - Staff: no access

All mutating views write an AuditLog entry inside the same transaction as the
mutation, through the service layer (billing/services.py).
"""

import csv
from decimal import Decimal
import logging

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import DatabaseError, models
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import DetailView, FormView, ListView, TemplateView, View

from academic.models import Class, ClassStatus, Session, Term
from core.mixins import RoleRequiredMixin, TenantScopedQuerysetMixin
from core.services import write_audit_log

from .forms import (
    ApplyCreditForm,
    FeeStructureForm,
    FeeStructureItemFormSet,
    PaymentForm,
    PaymentReversalForm,
    StudentFeeAdjustmentForm,
)
from .models import (
    CreditTransaction,
    FeeStructure,
    FeeStructureItem,
    Payment,
    PaymentItemAllocation,
    PaymentMethod,
    PaymentStatus,
    Receipt,
    StudentFeeAssignment,
    StudentFeeItem,
)
from .services import (
    adjust_student_fee,
    apply_student_credit,
    create_fee_structure,
    customize_student_fee_package,
    delete_fee_structure,
    record_payment,
    reverse_payment,
    sync_fee_structure_assignments,
    toggle_fee_structure_lock,
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
        from students.models import StudentEnrollment, StudentStatus

        context["items"] = self.object.items.all()
        context["can_manage"] = self.can_manage()
        context["assignment_count"] = self.object.assignments.count()
        context["total_class_students"] = (
            StudentEnrollment.unscoped.filter(
                institution_id=self.request.institution_id,
                klass=self.object.klass,
                session=self.object.session,
                student__status=StudentStatus.ACTIVE,
            )
            .values("student_id")
            .distinct()
            .count()
        )
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
        has_payments = Payment.objects.filter(
            assignment__fee_structure=fee_structure
        ).exists()

        form = FeeStructureForm(
            instance=fee_structure,
            institution_id=request.institution_id,
        )
        formset = FeeStructureItemFormSet(
            instance=fee_structure, prefix="items"
        )
        return self.render_to_response(
            self.get_context_data(
                form=form,
                formset=formset,
                fee_structure=fee_structure,
                has_payments=has_payments,
            )
        )

    def post(self, request, *args, **kwargs):
        fee_structure = self.get_object()
        has_payments = Payment.objects.filter(
            assignment__fee_structure=fee_structure
        ).exists()

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
                            "is_mandatory": item_form.cleaned_data.get("is_mandatory", True),
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


class FeeStructureToggleLockView(RoleRequiredMixin, View):
    """`/fee-structures/<id>/toggle-lock/` — toggle lock status."""

    module = "fee_structures"
    module_action = "manage"

    def get(self, request, pk, *args, **kwargs):
        return redirect("billing:fee_structure_detail", pk=pk)

    def post(self, request, pk, *args, **kwargs):
        fee_structure = get_object_or_404(
            FeeStructure.objects.filter(institution_id=request.institution_id),
            pk=pk,
        )
        try:
            toggle_fee_structure_lock(
                fee_structure=fee_structure,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            status_str = "locked" if fee_structure.locked else "unlocked"
            messages.success(
                request,
                f"Fee structure '{fee_structure.name}' is now {status_str}.",
            )
        except Exception as e:
            logger.exception("Toggle lock failed for fee structure %s", pk)
            messages.error(request, f"Could not update lock status: {e}")
        return redirect("billing:fee_structure_detail", pk=fee_structure.pk)


class FeeStructureSyncAssignmentsView(RoleRequiredMixin, View):
    """`POST /fee-structures/<id>/sync-assignments/` — Assign all unassigned active students in this class."""

    module = "fee_structures"
    module_action = "manage"

    def get(self, request, pk, *args, **kwargs):
        return redirect("billing:fee_structure_detail", pk=pk)

    def post(self, request, pk, *args, **kwargs):
        fee_structure = get_object_or_404(
            FeeStructure.objects.filter(
                institution_id=request.institution_id
            ).select_related("klass", "session", "term"),
            pk=pk,
        )
        try:
            assigned_count = sync_fee_structure_assignments(
                fee_structure=fee_structure,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            if assigned_count > 0:
                messages.success(
                    request,
                    f"Successfully assigned '{fee_structure.name}' to {assigned_count} student(s) in {fee_structure.klass.name}.",
                )
            else:
                messages.info(
                    request,
                    f"All active students in {fee_structure.klass.name} already have this fee structure assigned.",
                )
        except Exception as e:
            logger.exception("Fee structure assignment sync failed")
            messages.error(request, f"Could not sync student assignments: {e}")
        return redirect("billing:fee_structure_detail", pk=fee_structure.pk)


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
# Fee Assignments & Student Packages
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


class StudentFeePackageCustomizeView(RoleRequiredMixin, TemplateView):
    """`/fee-assignments/<id>/customize/` or `/students/<student_id>/fee-package/create/` —
    Interactive individual student fee package builder & customization workflow.
    """

    template_name = "billing/student_fee_package_form.html"
    module = "fee_structures"
    module_action = "manage"

    def dispatch(self, request, *args, **kwargs):
        assignment_id = kwargs.get("pk")
        student_id = kwargs.get("student_id")
        self.assignment = None
        self.student = None
        self.fee_structure = None

        if assignment_id:
            self.assignment = get_object_or_404(
                StudentFeeAssignment.objects.filter(
                    institution_id=request.institution_id
                ).select_related(
                    "student", "fee_structure", "fee_structure__klass",
                    "fee_structure__session", "fee_structure__term"
                ),
                pk=assignment_id,
            )
            self.student = self.assignment.student
            self.fee_structure = self.assignment.fee_structure
        elif student_id:
            from students.models import Student
            self.student = get_object_or_404(
                Student.objects.filter(institution_id=request.institution_id),
                pk=student_id,
            )
            structure_id = request.GET.get("structure_id")
            if structure_id:
                self.fee_structure = get_object_or_404(
                    FeeStructure.objects.filter(institution_id=request.institution_id),
                    pk=structure_id,
                )
            else:
                from students.views import current_enrollment
                enr = current_enrollment(self.student)
                if enr:
                    self.fee_structure = FeeStructure.objects.filter(
                        institution_id=request.institution_id,
                        klass=enr.klass,
                        session=enr.session,
                        term=enr.term,
                        is_active=True,
                    ).first()

            if self.fee_structure:
                self.assignment, _ = StudentFeeAssignment.unscoped.get_or_create(
                    institution_id=request.institution_id,
                    student=self.student,
                    fee_structure=self.fee_structure,
                    defaults={"amount_due": self.fee_structure.total_amount},
                )
                from .services import _populate_student_fee_items
                _populate_student_fee_items(self.assignment)

        if not self.assignment:
            messages.error(request, "No applicable fee structure found to customize for this student.")
            return redirect("students:payments", pk=self.student.pk if self.student else 1)

        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["assignment"] = self.assignment
        context["student"] = self.student
        context["fee_structure"] = self.fee_structure

        if not self.assignment.items.exists():
            from .services import _populate_student_fee_items
            _populate_student_fee_items(self.assignment)

        context["items"] = self.assignment.items.all().order_by("id")
        return context

    def post(self, request, *args, **kwargs):
        reason = request.POST.get("reason", "").strip()
        if not reason:
            messages.error(request, "A reason is required for customizing a student fee package.")
            return self.render_to_response(self.get_context_data())

        items_data = []
        item_ids = request.POST.getlist("item_id")
        for i_id in item_ids:
            name = request.POST.get(f"item_name_{i_id}", "").strip()
            orig_amount_val = request.POST.get(f"item_orig_amount_{i_id}", "0.00")
            orig_amount = Decimal(orig_amount_val) if orig_amount_val else Decimal("0.00")
            is_mandatory = request.POST.get(f"item_is_mandatory_{i_id}") == "1"
            is_included = request.POST.get(f"item_is_included_{i_id}") == "1"
            adj_type = request.POST.get(f"item_adj_type_{i_id}", "standard")
            discount_val = request.POST.get(f"item_discount_{i_id}", "0.00")
            discount = Decimal(discount_val) if discount_val else Decimal("0.00")
            notes = request.POST.get(f"item_notes_{i_id}", "").strip()
            fsi_id = request.POST.get(f"item_fsi_id_{i_id}")

            if not is_included:
                amount = Decimal("0.00")
            elif adj_type in ("discount", "exemption"):
                amount = max(Decimal("0.00"), orig_amount - discount)
            else:
                amount_val = request.POST.get(f"item_amount_{i_id}", str(orig_amount))
                amount = Decimal(amount_val) if amount_val else orig_amount

            items_data.append({
                "name": name,
                "amount": amount,
                "original_amount": orig_amount,
                "is_mandatory": is_mandatory,
                "is_included": is_included,
                "adjustment_type": adj_type,
                "discount_amount": discount,
                "notes": notes,
                "fee_structure_item_id": int(fsi_id) if fsi_id and fsi_id.isdigit() else None,
            })

        # Process additional custom items
        extra_names = request.POST.getlist("extra_name[]")
        extra_amounts = request.POST.getlist("extra_amount[]")
        extra_notes = request.POST.getlist("extra_notes[]")

        for idx, e_name in enumerate(extra_names):
            e_name = e_name.strip()
            if not e_name:
                continue
            e_amt_str = extra_amounts[idx] if idx < len(extra_amounts) else "0.00"
            e_amt = Decimal(e_amt_str) if e_amt_str else Decimal("0.00")
            e_note = extra_notes[idx] if idx < len(extra_notes) else ""

            items_data.append({
                "name": e_name,
                "amount": e_amt,
                "original_amount": e_amt,
                "is_mandatory": True,
                "is_included": True,
                "adjustment_type": "additional",
                "discount_amount": Decimal("0.00"),
                "notes": e_note,
                "fee_structure_item_id": None,
            })

        try:
            customize_student_fee_package(
                assignment=self.assignment,
                items_data=items_data,
                reason=reason,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                request,
                f"Fee package customized for {self.student.full_name}. New package total: ₦{self.assignment.amount_due}.",
            )
            return redirect("students:payments", pk=self.student.pk)
        except ValidationError as e:
            messages.error(request, str(e.message))
        except Exception as e:
            logger.exception("Customizing student fee package failed")
            messages.error(request, f"Failed to customize package: {e}")

        return self.render_to_response(self.get_context_data())


class StudentCreditApplyView(RoleRequiredMixin, View):
    """`POST /students/<id>/credit/apply/` — apply credit balance directly to an assignment."""

    module = "payments"
    module_action = "manage"

    def post(self, request, pk=None, student_id=None, *args, **kwargs):
        from students.models import Student
        s_id = student_id or pk or kwargs.get("student_id") or kwargs.get("pk")
        student = get_object_or_404(
            Student.objects.filter(institution_id=request.institution_id),
            pk=s_id,
        )
        form = ApplyCreditForm(request.POST, student=student, institution_id=request.institution_id)
        if form.is_valid():
            try:
                apply_student_credit(
                    student=student,
                    assignment=form.cleaned_data["assignment"],
                    amount=form.cleaned_data["amount"],
                    actor=request.user,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
                messages.success(
                    request,
                    f"₦{form.cleaned_data['amount']} credit applied successfully to {form.cleaned_data['assignment'].fee_structure.name}.",
                )
            except ValidationError as e:
                messages.error(request, str(e.message))
            except Exception as e:
                logger.exception("Credit application failed")
                messages.error(request, f"Could not apply credit: {e}")
        else:
            first_err = next(iter(form.errors.values()))[0] if form.errors else "Invalid submission."
            messages.error(request, f"Credit application error: {first_err}")

        next_url = request.POST.get("next") or request.GET.get("next")
        if next_url:
            return redirect(next_url)
        return redirect("students:payments", pk=student.pk)


# --------------------------------------------------------------------------- #
# Payments & Filtering
# --------------------------------------------------------------------------- #
def filter_payments_queryset(queryset, get_params):
    date_from = get_params.get("date_from")
    date_to = get_params.get("date_to")
    method = get_params.get("method")
    status = get_params.get("status")
    class_id = get_params.get("class_id")
    session_id = get_params.get("session_id")
    term_id = get_params.get("term_id")
    fee_structure_id = get_params.get("fee_structure_id")
    q = get_params.get("q", "").strip()

    if date_from:
        queryset = queryset.filter(payment_date__gte=date_from)
    if date_to:
        queryset = queryset.filter(payment_date__lte=date_to)
    if method:
        queryset = queryset.filter(method=method)
    if status:
        queryset = queryset.filter(status=status)
    if class_id:
        queryset = queryset.filter(assignment__fee_structure__klass_id=class_id)
    if session_id:
        queryset = queryset.filter(assignment__fee_structure__session_id=session_id)
    if term_id:
        queryset = queryset.filter(assignment__fee_structure__term_id=term_id)
    if fee_structure_id:
        queryset = queryset.filter(assignment__fee_structure_id=fee_structure_id)
    if q:
        queryset = queryset.filter(
            Q(assignment__student__first_name__icontains=q)
            | Q(assignment__student__last_name__icontains=q)
            | Q(assignment__student__admission_number__icontains=q)
            | Q(receipt__receipt_number__icontains=q)
        )
    return queryset


class PaymentListView(
    RoleRequiredMixin, TenantScopedQuerysetMixin, ListView
):
    """`/payments/` — comprehensive filterable payment list."""

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
                "assignment__fee_structure__session",
                "assignment__fee_structure__term",
                "recorded_by",
                "receipt",
            )
        )
        return filter_payments_queryset(queryset, self.request.GET)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_manage"] = self.can_manage()
        context["method_choices"] = Payment._meta.get_field("method").choices
        context["status_choices"] = Payment._meta.get_field("status").choices
        context["classes"] = Class.objects.filter(
            institution_id=self.request.institution_id, status=ClassStatus.ACTIVE
        ).order_by("order", "name")
        context["sessions"] = Session.objects.filter(
            institution_id=self.request.institution_id
        ).order_by("-start_date")
        context["terms"] = Term.objects.filter(
            institution_id=self.request.institution_id
        ).order_by("session__start_date", "start_date")
        context["fee_structures"] = FeeStructure.objects.filter(
            institution_id=self.request.institution_id, is_active=True
        ).order_by("-created_at")

        context["selected_date_from"] = self.request.GET.get("date_from", "")
        context["selected_date_to"] = self.request.GET.get("date_to", "")
        context["selected_method"] = self.request.GET.get("method", "")
        context["selected_status"] = self.request.GET.get("status", "")
        context["selected_class_id"] = self.request.GET.get("class_id", "")
        context["selected_session_id"] = self.request.GET.get("session_id", "")
        context["selected_term_id"] = self.request.GET.get("term_id", "")
        context["selected_fee_structure_id"] = self.request.GET.get("fee_structure_id", "")
        context["selected_q"] = self.request.GET.get("q", "")

        # Summary of filtered payments
        filtered_qs = self.get_queryset()
        active_filtered = filtered_qs.filter(status=PaymentStatus.ACTIVE)
        context["total_collected"] = (
            active_filtered.order_by().aggregate(total=models.Sum("amount"))["total"]
            or Decimal("0.00")
        )
        context["total_payments_count"] = filtered_qs.count()
        return context


class PaymentExportCSVView(RoleRequiredMixin, View):
    """`/payments/export/` — Stream filtered payments as CSV."""

    module = "payments"

    def get(self, request, *args, **kwargs):
        qs = (
            Payment.objects.filter(institution_id=request.institution_id)
            .select_related(
                "assignment__student",
                "assignment__fee_structure",
                "assignment__fee_structure__klass",
                "assignment__fee_structure__session",
                "assignment__fee_structure__term",
                "recorded_by",
                "receipt",
            )
            .order_by("-payment_date", "-created_at")
        )
        qs = filter_payments_queryset(qs, request.GET)

        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="adminpilot_payments_export.csv"'

        writer = csv.writer(response)
        writer.writerow([
            "Student Name",
            "Admission Number",
            "Class",
            "Guardian Name",
            "Guardian Phone",
            "Payment Date",
            "Amount (NGN)",
            "Payment Method",
            "Receipt Number",
            "Fee Package",
            "Session",
            "Term",
            "Status",
            "Recorded By",
        ])

        for p in qs:
            student = p.assignment.student
            fee_struct = p.assignment.fee_structure
            receipt_no = p.receipt.receipt_number if hasattr(p, "receipt") and p.receipt else "—"
            recorded_name = p.recorded_by.full_name if p.recorded_by else "System"

            writer.writerow([
                student.full_name,
                student.admission_number,
                fee_struct.klass.name if fee_struct else "—",
                student.guardian_name,
                student.guardian_phone,
                p.payment_date.strftime("%Y-%m-%d"),
                str(p.amount),
                p.get_method_display(),
                receipt_no,
                fee_struct.name if fee_struct else "—",
                fee_struct.session.name if fee_struct and fee_struct.session else "—",
                fee_struct.term.name if fee_struct and fee_struct.term else "—",
                p.get_status_display(),
                recorded_name,
            ])

        return response



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
        context["can_manage"] = self.can_manage()
        context["fee_assignments"] = (
            StudentFeeAssignment.objects.filter(
                institution_id=self.request.institution_id, student=student
            )
            .select_related("fee_structure", "fee_structure__term")
            .order_by("-id")
        )

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
