"""
Student views: the list with its filters, add/edit, the tabbed profile, and the
archive/reactivate state changes. Bulk import lives in students/imports.py — a
separate flow with its own file, kept apart so this module stays the everyday
single-student CRUD.

Two things here are load-bearing beyond ordinary CRUD:

  * Class lives on the enrollment, not the student. Add and edit collect a
    `klass` on the form and hand it to students.services, which writes the
    append-only enrollment row. The view never sets a class attribute on Student
    because there isn't one.
  * Bursar field-level restriction (docs/04_Permission_Matrix.md) is enforced
    here, in the context the view builds — not by hiding fields in the template.
    A Bursar's profile page never receives date of birth, address, email or
    parent names in its context at all, so there is nothing for the template to
    leak.
"""

import logging

from django.contrib import messages
from django.db import DatabaseError, transaction
from django.db.models import OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from academic.models import Class, ClassStatus, Session, Term
from core.mixins import RoleRequiredMixin, TenantScopedQuerysetMixin
from core.services import write_audit_log

from .forms import StudentForm
from .models import Student, StudentEnrollment, StudentStatus
from .services import change_student_class, create_student, delete_student

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def current_enrollment(student):
    """The student's most recent enrollment — their current class — or None.

    Enrollments are append-only, so "current" is simply the latest row by
    enrolled_at rather than a flag that has to be kept in sync.
    """
    return (
        StudentEnrollment.objects.filter(student=student)
        .select_related("klass", "session", "term")
        .order_by("-enrolled_at")
        .first()
    )


def build_overview_rows(student, klass, *, restricted):
    """Label/value rows for the profile Overview, filtered by role.

    `restricted` is the Bursar view: docs/04 lets a Bursar see the class, the
    primary guardian contact and the balance, but not the date of birth,
    address, email or parent names. Those simply never enter the returned list,
    which is the whole point — the restriction is in the data handed to the
    template, not in template logic that could be edited to bypass it.
    """
    klass_name = klass.name if klass else "—"
    if restricted:
        return [
            ("Class", klass_name),
            ("Guardian name", student.guardian_name),
            ("Guardian phone", student.guardian_phone),
            ("Credit balance", student.credit_balance),
        ]
    return [
        ("Class", klass_name),
        ("Gender", student.get_gender_display()),
        ("Date of birth", student.date_of_birth),
        ("Guardian name", student.guardian_name),
        ("Guardian phone", student.guardian_phone),
        ("Guardian email", student.guardian_email or "—"),
        ("Father's name", student.father_name or "—"),
        ("Mother's name", student.mother_name or "—"),
        ("Address", student.address or "—"),
        ("Date of admission", student.date_of_admission or "—"),
        ("Credit balance", student.credit_balance),
    ]


class StudentFormKwargsMixin:
    """StudentForm needs the institution for its scoped class dropdown."""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["institution_id"] = self.request.institution_id
        return kwargs


# --------------------------------------------------------------------------- #
# List
# --------------------------------------------------------------------------- #
class StudentListView(RoleRequiredMixin, TenantScopedQuerysetMixin, ListView):
    """`/students/` — filterable list (?class_id=&status=&q=).

    Every column here (name, admission number, class, status) is Bursar-safe, so
    the same table serves every role; `can_manage` is what removes the add/edit
    controls for a Bursar, not a different template.
    """

    model = Student
    template_name = "students/student_list.html"
    context_object_name = "students"
    module = "students"
    paginate_by = 50

    def get_queryset(self):
        queryset = super().get_queryset()

        # A student's current class is their latest enrollment. A correlated
        # subquery keeps the whole list to one query instead of one per row.
        latest = StudentEnrollment.objects.filter(student=OuterRef("pk")).order_by(
            "-enrolled_at"
        )
        queryset = queryset.annotate(
            current_class_id=Subquery(latest.values("klass_id")[:1]),
            current_class_name=Subquery(latest.values("klass__name")[:1]),
        )

        status = self.request.GET.get("status") or StudentStatus.ACTIVE
        if status in (StudentStatus.ACTIVE, StudentStatus.INACTIVE):
            queryset = queryset.filter(status=status)
        # any other value (e.g. "all") intentionally applies no status filter

        class_id = self.request.GET.get("class_id")
        if class_id:
            queryset = queryset.filter(current_class_id=class_id)

        query = self.request.GET.get("q", "").strip()
        if query:
            queryset = queryset.filter(
                Q(first_name__icontains=query)
                | Q(middle_name__icontains=query)
                | Q(last_name__icontains=query)
                | Q(admission_number__icontains=query)
            )
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["can_manage"] = self.can_manage()
        context["classes"] = Class.objects.filter(status=ClassStatus.ACTIVE).order_by(
            "order", "name"
        )
        context["status_options"] = (
            ("active", "Active"),
            ("inactive", "Archived"),
            ("all", "All"),
        )
        # Echo the active filters back so the form stays populated and the
        # heading can describe what is being shown.
        context["selected_status"] = self.request.GET.get("status") or "active"
        context["selected_class_id"] = self.request.GET.get("class_id") or ""
        context["query"] = self.request.GET.get("q", "").strip()
        return context


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #
class StudentCreateView(
    RoleRequiredMixin, StudentFormKwargsMixin, TenantScopedQuerysetMixin, CreateView
):
    """`/students/add/` — Owner/Administrator only.

    A new student is enrolled into the institution's current session and term,
    so both must exist first. Without them there is no term to enroll into, and
    the view sends the user to set one rather than failing at save time.
    """

    model = Student
    form_class = StudentForm
    template_name = "students/student_form.html"
    module = "students"
    module_action = "manage"

    def _require_current_term(self):
        self.current_session = Session.objects.filter(is_current=True).first()
        self.current_term = Term.objects.filter(is_current=True).first()
        if not (self.current_session and self.current_term):
            messages.error(
                self.request,
                "Set a current session and term before adding students — a "
                "student has to be enrolled into one.",
            )
            return redirect("academic:structure")
        return None

    def get(self, request, *args, **kwargs):
        return self._require_current_term() or super().get(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        return self._require_current_term() or super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_term"] = self.current_term
        return context

    def form_valid(self, form):
        student = form.save(commit=False)
        try:
            create_student(
                institution=self.request.user.institution,
                student=student,
                klass=form.cleaned_data["klass"],
                session=self.current_session,
                term=self.current_term,
                actor=self.request.user,
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
        except DatabaseError:
            logger.exception("Student creation failed")
            messages.error(
                self.request,
                "Something went wrong saving that student. Please try again.",
            )
            return self.form_invalid(form)

        self.object = student
        messages.success(
            self.request,
            f"{student.full_name} added — admission number {student.admission_number}.",
        )
        return redirect("students:detail", pk=student.pk)


# --------------------------------------------------------------------------- #
# Profile (tabbed)
# --------------------------------------------------------------------------- #
class StudentProfileMixin(RoleRequiredMixin, TenantScopedQuerysetMixin, DetailView):
    """Common base for the four profile tabs.

    Fetching goes through the scoped, institution-filtered queryset, so a pk
    from another tenant is a 404, not a 403 — the student simply does not exist
    for this institution. `active_tab` drives which tab the shared template
    highlights and renders.
    """

    model = Student
    context_object_name = "student"
    template_name = "students/student_detail.html"
    module = "students"
    active_tab = "overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_tab"] = self.active_tab
        context["can_manage"] = self.can_manage()
        context["is_restricted"] = self.request.user.is_bursar
        context["current"] = current_enrollment(self.object)
        return context


class StudentDetailView(StudentProfileMixin):
    """`/students/<id>/` — Overview tab."""

    active_tab = "overview"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        klass = context["current"].klass if context["current"] else None
        context["overview_rows"] = build_overview_rows(
            self.object, klass, restricted=context["is_restricted"]
        )
        return context


class StudentEnrollmentsView(StudentProfileMixin):
    """`/students/<id>/enrollments/` — Enrollment History tab (real data).

    The append-only enrollment rows, newest first — this is the one profile tab
    with real data in Phase 4; payments and the timeline arrive in Phases 5/6.
    """

    active_tab = "enrollments"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["enrollments"] = (
            StudentEnrollment.objects.filter(student=self.object)
            .select_related("klass", "session", "term", "enrolled_by")
            .order_by("-enrolled_at")
        )
        return context


class StudentPaymentsView(StudentProfileMixin):
    """`/students/<id>/payments/` — Payment History tab (real data from Phase 5)."""

    active_tab = "payments"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from billing.models import Payment, StudentFeeAssignment

        context["fee_assignments"] = (
            StudentFeeAssignment.unscoped.filter(student=self.object)
            .select_related("fee_structure", "fee_structure__klass", "fee_structure__term")
            .order_by("-created_at")
        )
        context["payments"] = (
            Payment.unscoped.filter(
                assignment__student=self.object
            )
            .select_related(
                "assignment__fee_structure",
                "receipt",
                "recorded_by",
            )
            .order_by("-payment_date", "-created_at")
        )
        context["credit_balance"] = self.object.credit_balance
        return context


class StudentTimelineView(StudentProfileMixin):
    """`/students/<id>/timeline/` — Activity Timeline tab."""

    active_tab = "timeline"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        from .services import get_student_timeline

        context["timeline_events"] = get_student_timeline(self.object)
        return context


# --------------------------------------------------------------------------- #
# Edit
# --------------------------------------------------------------------------- #
class StudentUpdateView(
    RoleRequiredMixin, StudentFormKwargsMixin, TenantScopedQuerysetMixin, UpdateView
):
    """`/students/<id>/edit/` — Owner/Administrator only.

    Biographical edits save straight to the row. Changing the class does not
    overwrite anything — it appends a new enrollment (docs/03, the bidirectional
    fee re-check lives in the service), which needs a current term, so a class
    change without one is a form error rather than a silent no-op.
    """

    model = Student
    form_class = StudentForm
    template_name = "students/student_form.html"
    context_object_name = "student"
    module = "students"
    module_action = "manage"

    def get_initial(self):
        initial = super().get_initial()
        current = current_enrollment(self.object)
        if current:
            initial["klass"] = current.klass_id
        return initial

    def form_valid(self, form):
        new_klass = form.cleaned_data["klass"]
        current = current_enrollment(self.object)
        class_changed = current is None or current.klass_id != new_klass.pk
        bio_changed = [field for field in form.changed_data if field != "klass"]

        current_session = Session.objects.filter(is_current=True).first()
        current_term = Term.objects.filter(is_current=True).first()
        if class_changed and not (current_session and current_term):
            form.add_error(
                "klass",
                "Set a current session and term before moving a student to "
                "another class.",
            )
            return self.form_invalid(form)

        try:
            with transaction.atomic():
                student = form.save()
                if bio_changed:
                    write_audit_log(
                        institution_id=self.request.institution_id,
                        actor=self.request.user,
                        action="student.updated",
                        summary=f"Updated {student.full_name}'s details",
                        target_type="Student",
                        target_id=student.pk,
                        detail={"fields": bio_changed},
                        ip_address=self.request.META.get("REMOTE_ADDR"),
                    )
                if class_changed:
                    change_student_class(
                        student=student,
                        klass=new_klass,
                        session=current_session,
                        term=current_term,
                        actor=self.request.user,
                        ip_address=self.request.META.get("REMOTE_ADDR"),
                    )
        except DatabaseError:
            logger.exception("Student update failed for student %s", self.object.pk)
            messages.error(
                self.request,
                "Something went wrong saving those changes. Please try again.",
            )
            return self.form_invalid(form)

        self.object = student
        if bio_changed or class_changed:
            messages.success(self.request, f"{student.full_name}'s record updated.")
        return redirect("students:detail", pk=student.pk)


# --------------------------------------------------------------------------- #
# Archive / reactivate (POST-only)
# --------------------------------------------------------------------------- #
class StudentStatusChangeView(RoleRequiredMixin, View):
    """Shared body of archive and reactivate.

    POST only, like the class equivalents: a state change reachable by GET can
    be tripped by a prefetch or a crawler. Archiving is a soft delete — the row,
    its enrollments, and everything pointing at it stay; only `status` moves.
    """

    module = "students"
    module_action = "manage"
    target_status = None
    audit_action = None

    def post(self, request, pk):
        student = get_object_or_404(
            Student.objects.filter(institution_id=request.institution_id), pk=pk
        )

        if student.status == self.target_status:
            messages.info(request, self.already_message(student))
            return redirect("students:detail", pk=student.pk)

        try:
            with transaction.atomic():
                student.status = self.target_status
                student.save(update_fields=["status", "updated_at"])
                write_audit_log(
                    institution_id=request.institution_id,
                    actor=request.user,
                    action=self.audit_action,
                    summary=self.audit_summary(student),
                    target_type="Student",
                    target_id=student.pk,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
        except DatabaseError:
            logger.exception("Student status change failed for student %s", pk)
            messages.error(
                request,
                "Something went wrong updating that student. Please try again.",
            )
            return redirect("students:detail", pk=student.pk)

        messages.success(request, self.done_message(student))
        return redirect("students:detail", pk=student.pk)


class StudentArchiveView(StudentStatusChangeView):
    """`/students/<id>/archive/` — soft-delete."""

    target_status = StudentStatus.INACTIVE
    audit_action = "student.archived"

    def already_message(self, student):
        return f"{student.full_name} is already archived."

    def audit_summary(self, student):
        return f"Archived {student.full_name} ({student.admission_number})"

    def done_message(self, student):
        return (
            f"{student.full_name} archived. Their record and history are kept — "
            f"reactivate them any time."
        )


class StudentReactivateView(StudentStatusChangeView):
    """`/students/<id>/reactivate/` — restore an archived student."""

    target_status = StudentStatus.ACTIVE
    audit_action = "student.reactivated"

    def already_message(self, student):
        return f"{student.full_name} is already active."

    def audit_summary(self, student):
        return f"Reactivated {student.full_name} ({student.admission_number})"

    def done_message(self, student):
        return f"{student.full_name} is active again."


class StudentDeleteView(RoleRequiredMixin, TenantScopedQuerysetMixin, TemplateView):
    """`/students/<id>/delete/` — delete a student with optional purge of billing records."""

    template_name = "students/student_confirm_delete.html"
    module = "students"
    module_action = "manage"

    def get_object(self):
        return get_object_or_404(
            Student.objects.filter(institution_id=self.request.institution_id),
            pk=self.kwargs["pk"],
        )

    def get(self, request, *args, **kwargs):
        student = self.get_object()
        enrollment = current_enrollment(student)
        assignment_count = student.fee_assignments.count()
        payment_count = sum(
            assignment.payments.count() for assignment in student.fee_assignments.all()
        )

        return self.render_to_response(
            self.get_context_data(
                student=student,
                current_class=enrollment.klass if enrollment else None,
                assignment_count=assignment_count,
                payment_count=payment_count,
                credit_balance=student.credit_balance,
            )
        )

    def post(self, request, *args, **kwargs):
        student = self.get_object()
        student_name = student.full_name
        # Choice: keep_records = 'on' or '1' if user chooses to keep payment records
        keep_financial_records = request.POST.get("keep_records") == "on"

        try:
            result = delete_student(
                student=student,
                keep_financial_records=keep_financial_records,
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
            if result["action"] == "deleted":
                messages.success(
                    request,
                    f"Student '{student_name}' and all associated records were permanently deleted.",
                )
            else:
                messages.info(
                    request,
                    f"Student '{student_name}' was archived to preserve financial records.",
                )
            return redirect("students:list")
        except DatabaseError:
            logger.exception("Student deletion failed for %s", student.pk)
            messages.error(
                request, "Something went wrong deleting the student record."
            )
            return redirect("students:detail", pk=student.pk)

