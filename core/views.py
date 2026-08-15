"""
Institution-scoped core views: the Dashboard, the first-run Setup Wizard,
Institution Settings, and the Audit Log.
"""

import logging
import zoneinfo

from django.contrib import messages
from django.db import DatabaseError, transaction
from django.shortcuts import redirect
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.generic import ListView, TemplateView, UpdateView

from academic.forms import SessionForm, SetupClassesForm, TermForm
from academic.models import Class, ClassStatus, Session, Term
from academic.services import create_classes, create_first_session_and_term
from core.forms import InstitutionSettingsForm
from core.mixins import RoleRequiredMixin, TenantScopedQuerysetMixin
from core.models import AuditLog, Institution
from core.services import write_audit_log

logger = logging.getLogger(__name__)


class DashboardView(RoleRequiredMixin, TemplateView):
    """`/` — the post-login landing page.

    Everything is computed here, in one context, server-side: docs/08_UI_UX.md is
    explicit that the Action Required counts are not assembled by JS.

    Bursar gets a genuinely reduced context, not a filtered template. The student
    and class counts are never added to the context for a Bursar request, so the
    numbers are not merely hidden — they were never sent.
    """

    template_name = "core/dashboard.html"
    module = "dashboard"

    def check_access(self, request):
        blocked = super().check_access(request)
        if blocked is not None:
            return blocked

        # A freshly-approved Owner has no classes and no term; an empty dashboard
        # would be a dead end. Redirecting from the access check rather than from
        # LoginView means every entry point behaves the same — a bookmark, a
        # password-reset return, a stale tab.
        if request.user.is_owner and not Class.objects.exists():
            return redirect("core:setup")
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        from decimal import Decimal

        from django.db.models import Sum

        from billing.models import Payment, PaymentStatus, StudentFeeAssignment

        context["greeting"] = self._greeting(user)
        context["today"] = timezone.localtime()
        current_term = Term.objects.filter(is_current=True).first()
        context["current_term"] = current_term

        context["financials_available"] = True
        context["can_manage"] = self.can_manage()

        # Fees Due & Collected
        assignments_qs = StudentFeeAssignment.unscoped.filter(
            institution_id=self.request.institution_id
        )
        payments_qs = Payment.unscoped.filter(
            institution_id=self.request.institution_id,
            status=PaymentStatus.ACTIVE,
        )

        if current_term:
            assignments_qs = assignments_qs.filter(fee_structure__term=current_term)
            payments_qs = payments_qs.filter(
                assignment__fee_structure__term=current_term
            )

        fees_due = assignments_qs.aggregate(total=Sum("amount_due"))["total"] or Decimal("0.00")
        fees_collected = payments_qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

        today_date = timezone.localtime().date()
        today_payments = Payment.unscoped.filter(
            institution_id=self.request.institution_id,
            status=PaymentStatus.ACTIVE,
            payment_date=today_date,
        )
        received_today = today_payments.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

        # Action Required: fee assignments overdue (>30 days since creation with balance > 0)
        thirty_days_ago = timezone.now() - timezone.timedelta(days=30)
        action_required_count = StudentFeeAssignment.unscoped.filter(
            institution_id=self.request.institution_id,
            created_at__lte=thirty_days_ago,
        ).exclude(amount_due=0).count()

        context["fees_due"] = fees_due
        context["fees_collected"] = fees_collected
        context["received_today"] = received_today
        context["action_required_count"] = action_required_count

        context["recent_payments"] = (
            Payment.unscoped.filter(
                institution_id=self.request.institution_id,
                status=PaymentStatus.ACTIVE,
            )
            .select_related(
                "assignment__student",
                "assignment__fee_structure",
                "receipt",
            )
            .order_by("-created_at")[:6]
        )

        if not user.is_bursar:
            context["student_count"] = self._student_count()
            context["class_count"] = Class.objects.filter(
                status=ClassStatus.ACTIVE
            ).count()

        context["recent_activity"] = (
            AuditLog.objects.select_related("actor")[:8] if user.is_owner else []
        )
        return context

    def _student_count(self):
        from students.models import Student, StudentStatus

        return Student.objects.filter(status=StudentStatus.ACTIVE).count()

    def _greeting(self, user):
        try:
            tz = zoneinfo.ZoneInfo(user.institution.timezone)
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            tz = timezone.get_current_timezone()

        hour = timezone.localtime(timezone=tz).hour
        if hour < 12:
            part = "Good morning"
        elif hour < 17:
            part = "Good afternoon"
        else:
            part = "Good evening"
        first_name = user.full_name.split()[0] if user.full_name else ""
        return f"{part}, {first_name}".rstrip(", ")


# --------------------------------------------------------------------------- #
# Global Search
# --------------------------------------------------------------------------- #
class SearchView(RoleRequiredMixin, TemplateView):
    """`/search/?q=` — Full page search results.

    Searches across Students, Payments, and Receipts. Field-level Bursar restrictions
    are applied in the search service logic.
    """

    template_name = "core/search_results.html"
    module = "search"

    def get_context_data(self, **kwargs):
        from core.search import search_all

        context = super().get_context_data(**kwargs)
        query = self.request.GET.get("q", "").strip()
        context["query"] = query
        context["results"] = search_all(
            institution_id=self.request.institution_id,
            query=query,
            is_bursar=self.request.user.is_bursar,
        )
        return context


class SearchSuggestView(RoleRequiredMixin, TemplateView):
    """`/search/suggest/?q=` — Search-as-you-type JSON endpoint.

    The ONLY JsonResponse endpoint in V1 per docs/03_Views_and_Endpoints.md.
    """

    module = "search"

    def get(self, request, *args, **kwargs):
        from django.http import JsonResponse

        from core.search import search_suggestions

        query = request.GET.get("q", "").strip()
        suggestions = search_suggestions(
            institution_id=request.institution_id,
            query=query,
        )
        return JsonResponse({"suggestions": suggestions})


class InstitutionSetupWizardView(RoleRequiredMixin, TemplateView):
    """`/setup/` — the first-run wizard. Owner only.

    Three steps, three full page loads, one URL. Each step POSTs back here and
    redirects back here, and the step to show is derived from what actually exists
    in the database rather than from a step counter in the session. That makes it
    resumable: an Owner who closes the tab after creating their session comes back
    to the class step, not to the start, and re-POSTing a step cannot create a
    second session.

    The details step is the exception — it edits a row that always exists, so
    there is nothing in the database that says "they have seen this screen." A
    single session flag marks it. Losing that flag costs the Owner one extra look
    at a pre-filled form, which is the mildest possible failure here.
    """

    module = "institution_settings"
    module_action = "manage"
    templates = {
        "details": "core/setup_details.html",
        "academic": "core/setup_academic.html",
        "classes": "core/setup_classes.html",
    }
    DETAILS_DONE_KEY = "setup_details_done"

    STEP_ORDER = ("details", "academic", "classes")

    def current_step(self):
        if not Session.objects.exists():
            if self.request.session.get(self.DETAILS_DONE_KEY):
                return "academic"
            return "details"
        if not Class.objects.exists():
            return "classes"
        return "done"

    def get_template_names(self):
        return [self.templates[self.step]]

    def check_access(self, request):
        blocked = super().check_access(request)
        if blocked is not None:
            return blocked

        self.step = self.current_step()
        if self.step == "done":
            # Nothing left to configure. Coming back here after setup should not
            # offer to redo it.
            return redirect("core:dashboard")
        return None

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["step"] = self.step
        context["step_number"] = self.STEP_ORDER.index(self.step) + 1
        context["step_total"] = len(self.STEP_ORDER)
        context.setdefault("form", self.build_form())
        if self.step == "academic":
            context.setdefault("term_form", self.build_term_form())
        return context

    def build_form(self, data=None):
        institution = Institution.objects.get(pk=self.request.institution_id)
        if self.step == "details":
            return InstitutionSettingsForm(data=data, instance=institution)
        if self.step == "academic":
            return SessionForm(data=data, institution_id=self.request.institution_id)
        return SetupClassesForm(data=data, institution_id=self.request.institution_id)

    def build_term_form(self, data=None):
        """The first term, collected on the same screen as its session.

        A wizard that created a session and then asked separately for a term would
        let an Owner stop in between and land in the one state that blocks
        everything downstream: enrolment and fee structures both require a term.

        Prefixed, because SessionForm and TermForm both have `name`, `start_date`
        and `end_date` — unprefixed they would render as one set of inputs and each
        form would read the other's values.

        `session` is dropped because the session being created on this screen is
        the answer; there is nothing to choose between.
        """
        form = TermForm(
            data=data, institution_id=self.request.institution_id, prefix="term"
        )
        del form.fields["session"]
        return form

    def post(self, request, *args, **kwargs):
        handler = getattr(self, f"save_{self.step}")
        return handler(request)

    def save_details(self, request):
        form = self.build_form(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        with transaction.atomic():
            institution = form.save()
            if form.changed_data:
                write_audit_log(
                    institution_id=institution.pk,
                    actor=request.user,
                    action="institution.updated",
                    summary="Completed setup step 1: institution details",
                    target_type="Institution",
                    target_id=institution.pk,
                    detail={"fields": list(form.changed_data)},
                    ip_address=request.META.get("REMOTE_ADDR"),
                )

        request.session[self.DETAILS_DONE_KEY] = True
        return redirect("core:setup")

    def save_academic(self, request):
        session_form = self.build_form(request.POST)
        term_form = self.build_term_form(request.POST)

        # Both validated before either is saved: a valid session with an invalid
        # term must not leave the session behind, or the next GET would skip
        # straight past this step with no term in existence.
        session_ok = session_form.is_valid()
        term_ok = term_form.is_valid()
        if not (session_ok and term_ok):
            return self.render_to_response(
                self.get_context_data(form=session_form, term_form=term_form)
            )

        term_data = term_form.cleaned_data
        if term_data["start_date"] < session_form.cleaned_data["start_date"] or (
            term_data["end_date"] > session_form.cleaned_data["end_date"]
        ):
            # TermForm's own session-range check cannot run here: its `session`
            # field is gone, and the session it would compare against does not
            # exist yet.
            term_form.add_error(
                None, "The term's dates must fall inside the session's dates."
            )
            return self.render_to_response(
                self.get_context_data(form=session_form, term_form=term_form)
            )

        try:
            create_first_session_and_term(
                request.institution_id,
                session_form,
                term_name=term_data["name"],
                term_start=term_data["start_date"],
                term_end=term_data["end_date"],
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except DatabaseError:
            logger.exception(
                "Setup wizard failed creating first session for institution %s",
                request.institution_id,
            )
            messages.error(
                request, "Something went wrong saving that. Please try again."
            )
            return redirect("core:setup")

        return redirect("core:setup")

    def save_classes(self, request):
        form = self.build_form(request.POST)
        if not form.is_valid():
            return self.render_to_response(self.get_context_data(form=form))

        try:
            created = create_classes(
                request.institution_id,
                form.cleaned_data["names"],
                actor=request.user,
                ip_address=request.META.get("REMOTE_ADDR"),
            )
        except DatabaseError:
            logger.exception(
                "Setup wizard failed creating classes for institution %s",
                request.institution_id,
            )
            messages.error(
                request, "Something went wrong saving those classes. Please try again."
            )
            return redirect("core:setup")

        messages.success(
            request,
            f"Setup complete — {len(created)} "
            f"class{'' if len(created) == 1 else 'es'} added. "
            f"You can start adding students now.",
        )
        return redirect("core:dashboard")


class InstitutionSettingsView(RoleRequiredMixin, UpdateView):
    """`/settings/institution/` — Owner only.

    The `code` field is editable only until the first receipt exists
    (docs/02_Database.md). The rule lives in the form, so a locked code comes back
    as an inline field error — what docs/03_Views_and_Endpoints.md specifies, not
    a 403 and not a silently discarded value.
    """

    template_name = "core/institution_settings.html"
    form_class = InstitutionSettingsForm
    module = "institution_settings"
    module_action = "manage"
    success_url = reverse_lazy("core:institution_settings")
    context_object_name = "institution"

    def get_object(self, queryset=None):
        # Never from a URL segment: the institution is the one on the session.
        # Institution is not tenant-scoped (it *is* the tenant), so a pk lookup
        # is correct here — and the pk comes from the request, not the client.
        return Institution.objects.get(pk=self.request.institution_id)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["code_is_locked"] = context["form"].code_is_locked()
        return context

    def form_valid(self, form):
        changed = list(form.changed_data)
        with transaction.atomic():
            response = super().form_valid(form)
            if changed:
                write_audit_log(
                    institution_id=self.request.institution_id,
                    actor=self.request.user,
                    action="institution.updated",
                    summary=f"Updated institution settings: {', '.join(changed)}",
                    target_type="Institution",
                    target_id=self.object.pk,
                    detail={"fields": changed},
                    ip_address=self.request.META.get("REMOTE_ADDR"),
                )
        messages.success(
            self.request,
            "Institution settings saved." if changed else "No changes to save.",
        )
        return response


class AuditLogView(RoleRequiredMixin, TenantScopedQuerysetMixin, ListView):
    """`/settings/audit-log/`.

    Tiered visibility (docs/04_Permission_Matrix.md): Owner sees the whole
    institution's activity, everyone else sees only their own actions. The tiering
    is a queryset filter, not a template condition — an Administrator's response
    never contains another user's entries.
    """

    model = AuditLog
    template_name = "core/audit_log.html"
    context_object_name = "entries"
    paginate_by = 50
    module = "audit_log"

    def get_queryset(self):
        queryset = super().get_queryset().select_related("actor")
        if not self.request.user.is_owner:
            queryset = queryset.filter(actor=self.request.user)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["scope_is_own_only"] = not self.request.user.is_owner
        return context
