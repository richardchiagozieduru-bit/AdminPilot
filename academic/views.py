"""
Academic structure views: the Class list and its CRUD, plus the ongoing
Session/Term screen.

Class deactivation is the one rule worth reading before editing this file: it is
allowed while students are still enrolled. Deactivating a class means "stop
putting new students in here", not "this class never happened" — its historical
enrollments, fee assignments and receipts all keep pointing at it
(docs/03_Views_and_Endpoints.md, /classes/<id>/deactivate/). Nothing here should
start refusing deactivation because a class is occupied, and nothing should
cascade a delete from it.
"""

import logging

from django.contrib import messages
from django.db import DatabaseError, transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import (
    CreateView,
    DetailView,
    ListView,
    TemplateView,
    UpdateView,
    View,
)

from core.mixins import AuditedFormMixin, RoleRequiredMixin, TenantScopedQuerysetMixin
from core.services import write_audit_log
from students.models import StudentStatus

from .forms import ClassForm, SessionForm, TermForm
from .models import Class, ClassStatus, Session, Term
from .services import set_current_session, set_current_term

logger = logging.getLogger(__name__)


class ClassListView(RoleRequiredMixin, TenantScopedQuerysetMixin, ListView):
    """`/classes/` — list with the current student count per class.

    A Bursar reaches this read-only: "classes" grants view to every role and
    manage to Owner/Administrator, so the mixin lets a Bursar in and
    `can_manage` keeps the edit controls out of their page.
    """

    model = Class
    template_name = "academic/class_list.html"
    context_object_name = "classes"
    module = "classes"

    def get_queryset(self):
        queryset = super().get_queryset()
        term = Term.objects.filter(is_current=True).first()
        if term is None:
            # No current term means no enrollment can belong to one, so every
            # count is zero. Annotating without the term filter would instead
            # count every enrollment the class has ever had.
            return queryset
        return queryset.annotate(
            student_count=Count(
                "enrollments",
                filter=Q(
                    enrollments__term=term,
                    enrollments__student__status=StudentStatus.ACTIVE,
                ),
                distinct=True,
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["current_term"] = Term.objects.filter(is_current=True).first()
        context["can_manage"] = self.can_manage()
        context["active_count"] = sum(
            1 for klass in context["classes"] if klass.status == ClassStatus.ACTIVE
        )
        return context


class ClassDetailView(RoleRequiredMixin, TenantScopedQuerysetMixin, DetailView):
    """`/classes/<id>/` — class hub showing enrolled students, fee structures, and actions."""

    model = Class
    template_name = "academic/class_detail.html"
    context_object_name = "klass"
    module = "classes"

    def get_context_data(self, **kwargs):
        from billing.models import FeeStructure
        from students.models import StudentEnrollment, StudentStatus

        context = super().get_context_data(**kwargs)
        klass = self.object
        current_term = Term.objects.filter(
            institution_id=self.request.institution_id, is_current=True
        ).first()
        current_session = Session.objects.filter(
            institution_id=self.request.institution_id, is_current=True
        ).first()

        enrollment_filter = Q(
            institution_id=self.request.institution_id,
            klass=klass,
            student__status=StudentStatus.ACTIVE,
        )
        if current_session:
            enrollment_filter &= Q(session=current_session)

        enrollments = (
            StudentEnrollment.unscoped.filter(enrollment_filter)
            .select_related("student", "session", "term")
            .order_by("student__last_name", "student__first_name")
        )
        seen_student_ids = set()
        unique_enrollments = []
        for enr in enrollments:
            if enr.student_id not in seen_student_ids:
                seen_student_ids.add(enr.student_id)
                unique_enrollments.append(enr)

        context["enrollments"] = unique_enrollments
        context["student_count"] = len(unique_enrollments)
        context["current_term"] = current_term
        context["current_session"] = current_session
        context["fee_structures"] = (
            FeeStructure.objects.filter(
                institution_id=self.request.institution_id, klass=klass
            )
            .select_related("session", "term")
            .order_by("-created_at")
        )
        context["can_manage"] = self.can_manage()
        return context



class ClassFormKwargsMixin:
    """ClassForm needs the institution for its per-institution name check."""

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["institution_id"] = self.request.institution_id
        return kwargs


class ClassCreateView(
    RoleRequiredMixin, ClassFormKwargsMixin, AuditedFormMixin, CreateView
):
    """`/classes/add/`."""

    model = Class
    form_class = ClassForm
    template_name = "academic/class_form.html"
    module = "classes"
    module_action = "manage"
    success_url = reverse_lazy("academic:class_list")
    audit_action = "class.created"

    def audit_summary(self, obj):
        return f"Added class {obj.name}"

    def form_valid(self, form):
        form.instance.institution_id = self.request.institution_id
        with transaction.atomic():
            response = super().form_valid(form)
            self.log_audit(self.object)
        messages.success(self.request, f"Class “{self.object.name}” added.")
        return response


class ClassUpdateView(
    RoleRequiredMixin,
    ClassFormKwargsMixin,
    AuditedFormMixin,
    TenantScopedQuerysetMixin,
    UpdateView,
):
    """`/classes/<id>/edit/` — name, order, status."""

    model = Class
    form_class = ClassForm
    template_name = "academic/class_form.html"
    context_object_name = "klass"
    module = "classes"
    module_action = "manage"
    success_url = reverse_lazy("academic:class_list")
    audit_action = "class.updated"

    def audit_summary(self, obj):
        return f"Updated class {obj.name}"

    def form_valid(self, form):
        changed = list(form.changed_data)
        with transaction.atomic():
            response = super().form_valid(form)
            if changed:
                self.log_audit(self.object, detail={"fields": changed})
        if changed:
            messages.success(self.request, f"Class “{self.object.name}” updated.")
        return response


class ClassStatusChangeView(RoleRequiredMixin, View):
    """Shared body of deactivate and reactivate.

    POST only, both of them: a state change reachable by GET can be triggered by
    a link prefetch or a crawler following the page.
    """

    module = "classes"
    module_action = "manage"
    target_status = None
    audit_action = None

    def post(self, request, pk):
        klass = get_object_or_404(
            Class.objects.filter(institution_id=request.institution_id), pk=pk
        )

        if klass.status == self.target_status:
            messages.info(request, self.already_message(klass))
            return redirect("academic:class_list")

        try:
            with transaction.atomic():
                klass.status = self.target_status
                klass.save(update_fields=["status"])
                write_audit_log(
                    institution_id=request.institution_id,
                    actor=request.user,
                    action=self.audit_action,
                    summary=self.audit_summary(klass),
                    target_type="Class",
                    target_id=klass.pk,
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
        except DatabaseError:
            # CLAUDE.md: log server-side, hand back something generic.
            logger.exception("Class status change failed for class %s", pk)
            messages.error(
                request,
                "Something went wrong updating that class. Please try again.",
            )
            return redirect("academic:class_list")

        messages.success(request, self.done_message(klass))
        return redirect("academic:class_list")


class ClassDeactivateView(ClassStatusChangeView):
    """`/classes/<id>/deactivate/`.

    Enrolled students are not a reason to refuse — see the module docstring.
    """

    target_status = ClassStatus.INACTIVE
    audit_action = "class.deactivated"

    def already_message(self, klass):
        return f"“{klass.name}” is already inactive."

    def audit_summary(self, klass):
        return f"Deactivated class {klass.name}"

    def done_message(self, klass):
        return (
            f"“{klass.name}” is now inactive. Students already in it keep their "
            f"records — it just won't be offered for new enrollments."
        )


class ClassReactivateView(ClassStatusChangeView):
    """`/classes/<id>/reactivate/`.

    Not a row in docs/03_Views_and_Endpoints.md's table, but ClassUpdateView
    already lists `status` among its editable fields, so reactivation is reachable
    with or without this. Having it as its own POST means it carries an audit
    entry naming the operation, rather than showing up as a generic field edit.
    """

    target_status = ClassStatus.ACTIVE
    audit_action = "class.reactivated"

    def already_message(self, klass):
        return f"“{klass.name}” is already active."

    def audit_summary(self, klass):
        return f"Reactivated class {klass.name}"

    def done_message(self, klass):
        return f"“{klass.name}” is active again."


class AcademicStructureView(RoleRequiredMixin, TemplateView):
    """`/settings/academic/` — ongoing Session and Term creation. Owner only.

    One URL taking GET and POST, as the endpoint table specifies, with three POST
    actions distinguished by an `action` field in the submitted data. They all
    operate on the same small object graph this page is already showing, so
    splitting them across URLs would only mean three templates rendering the same
    list.
    """

    template_name = "academic/structure.html"
    module = "academic_structure"
    module_action = "manage"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # setdefault, so a POST that re-renders with its own bound form keeps it
        # instead of having a fresh blank one written over the errors.
        context.setdefault("session_form", self.build_session_form())
        context.setdefault("term_form", self.build_term_form())
        context["sessions"] = Session.objects.prefetch_related("terms")
        context["current_session"] = Session.objects.filter(is_current=True).first()
        context["current_term"] = Term.objects.filter(is_current=True).first()
        return context

    def build_session_form(self, data=None):
        return SessionForm(data=data, institution_id=self.request.institution_id)

    def build_term_form(self, data=None):
        return TermForm(data=data, institution_id=self.request.institution_id)

    def post(self, request, *args, **kwargs):
        handler = {
            "add_session": self.add_session,
            "add_term": self.add_term,
            "set_current_term": self.mark_current_term,
            "set_current_session": self.mark_current_session,
        }.get(request.POST.get("action"))

        if handler is None:
            messages.error(request, "Unrecognised action.")
            return redirect("academic:structure")
        return handler(request)

    def add_session(self, request):
        form = self.build_session_form(request.POST)
        if not form.is_valid():
            return self.render_to_response(
                self.get_context_data(session_form=form, open_form="session")
            )

        with transaction.atomic():
            session = form.save(commit=False)
            session.institution_id = request.institution_id
            session.save()
            write_audit_log(
                institution_id=request.institution_id,
                actor=request.user,
                action="session.created",
                summary=f"Created session {session.name}",
                target_type="Session",
                target_id=session.pk,
                ip_address=request.META.get("REMOTE_ADDR"),
            )

        # Deliberately not made current. A school entering next year's session in
        # March is still teaching this year's, and moving the dashboard's term
        # under them as a side effect of adding a row would be wrong.
        messages.success(
            request,
            f"Session “{session.name}” added. Add its terms below, then set the "
            f"current term when it starts.",
        )
        return redirect("academic:structure")

    def add_term(self, request):
        form = self.build_term_form(request.POST)
        if not form.is_valid():
            return self.render_to_response(
                self.get_context_data(term_form=form, open_form="term")
            )

        with transaction.atomic():
            term = form.save(commit=False)
            term.institution_id = request.institution_id
            term.save()
            write_audit_log(
                institution_id=request.institution_id,
                actor=request.user,
                action="term.created",
                summary=f"Created term {term.name} in {term.session.name}",
                target_type="Term",
                target_id=term.pk,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        messages.success(request, f"Term “{term.name}” added to {term.session.name}.")
        return redirect("academic:structure")

    def mark_current_term(self, request):
        term = get_object_or_404(
            Term.objects.filter(institution_id=request.institution_id).select_related(
                "session"
            ),
            pk=request.POST.get("term_id"),
        )
        set_current_term(
            request.institution_id,
            term,
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, f"{term.session.name} — {term.name} is now current.")
        return redirect("academic:structure")

    def mark_current_session(self, request):
        session = get_object_or_404(
            Session.objects.filter(institution_id=request.institution_id),
            pk=request.POST.get("session_id"),
        )
        set_current_session(
            request.institution_id,
            session,
            actor=request.user,
            ip_address=request.META.get("REMOTE_ADDR"),
        )
        messages.success(request, f"“{session.name}” is now the current session.")
        return redirect("academic:structure")
