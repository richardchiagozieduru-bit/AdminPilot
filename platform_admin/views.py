"""
Super Admin views.

Every view here runs with tenant context explicitly cleared
(no_institution_context). A Super Admin has no institution, so if a
tenant-scoped query ever appears in this app it returns zero rows rather than
inheriting whatever institution was stamped last on the pooled connection.
That is the structural half of docs/02_Database.md's checklist item 5; the
other half is the import list in platform_admin/services.py.
"""

import logging

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.core.exceptions import PermissionDenied
from django.db import DatabaseError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.generic import ListView, View

from core.models import Institution
from core.tenant import no_institution_context

from .forms import PlatformLoginForm, RejectInstitutionForm
from .models import PlatformUser
from .services import ApprovalError, approve_institution, reject_institution

logger = logging.getLogger(__name__)


class PlatformLoginView(auth_views.LoginView):
    """`/platform/login/` (CR-004).

    Its own URL and form so a non-tenant account never passes through
    tenant-resolution code. TenantContextMiddleware still runs, and correctly
    resolves a PlatformUser to no institution — a Super Admin session leaves
    SESSION_CONTEXT unstamped, which is what makes every tenant table return
    zero rows for them at the database level.
    """

    template_name = "platform_admin/login.html"
    authentication_form = PlatformLoginForm
    redirect_authenticated_user = True
    next_page = reverse_lazy("platform_admin:institution_list")

    def get_success_url(self):
        return str(self.next_page)


class PlatformLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("platform_admin:login")


class SuperAdminRequiredMixin:
    """Authenticated as a PlatformUser, with tenant context cleared.

    isinstance rather than a role check: Super Admin is a separate model, not a
    privileged role value (docs/02_Database.md), so this is the only honest
    test. An institution Owner holding a valid session fails it.
    """

    def dispatch(self, request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated:
            return redirect("platform_admin:login")
        if not isinstance(user, PlatformUser):
            raise PermissionDenied("Platform access requires a Super Admin account.")

        # Belt and braces over the middleware: any tenant-scoped query reachable
        # from a platform view returns nothing, whatever the connection was
        # stamped with before.
        with no_institution_context():
            return super().dispatch(request, *args, **kwargs)


class PendingInstitutionsListView(SuperAdminRequiredMixin, ListView):
    """`/platform/institutions/` — registrations by status.

    Institution carries no institution_id and is not bound to the tenant
    Security Policy, so listing every row here is not a cross-tenant read; it
    is the platform's own customer list. No student, fee, or payment column is
    reachable from this queryset.
    """

    template_name = "platform_admin/institution_list.html"
    context_object_name = "institutions"
    paginate_by = 50

    def get_queryset(self):
        queryset = Institution.objects.select_related("reviewed_by").only(
            "id", "name", "code", "type", "status", "email", "phone",
            "created_at", "reviewed_at", "review_note", "reviewed_by",
        )
        status = self.request.GET.get("status", Institution.Status.PENDING)
        if status in Institution.Status.values:
            queryset = queryset.filter(status=status)
        elif status != "all":
            # Unrecognised filter falls back to pending rather than showing
            # everything — the review queue is the point of the screen.
            queryset = queryset.filter(status=Institution.Status.PENDING)
        return queryset.order_by("created_at")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["active_status"] = self.request.GET.get(
            "status", Institution.Status.PENDING
        )
        context["status_choices"] = Institution.Status.choices
        context["reject_form"] = RejectInstitutionForm()
        context["pending_count"] = Institution.objects.filter(
            status=Institution.Status.PENDING
        ).count()
        return context


class ApproveInstitutionView(SuperAdminRequiredMixin, View):
    """POST `/platform/institutions/<id>/approve/`.

    POST-only: approval is a state change, so it must not be reachable by a
    prefetched link or a crawler following a GET.
    """

    def post(self, request, pk):
        institution = get_object_or_404(Institution, pk=pk)
        try:
            approve_institution(institution, reviewer=request.user)
        except ApprovalError as error:
            messages.error(request, str(error))
        except DatabaseError:
            logger.exception("Approving institution %s failed", pk)
            messages.error(
                request, "Something went wrong approving this school. Please retry."
            )
        else:
            messages.success(
                request,
                f"{institution.name} approved. Their Owner can now sign in.",
            )
        return redirect(self.success_url(request))

    def success_url(self, request):
        return request.POST.get("next") or reverse_lazy(
            "platform_admin:institution_list"
        )


class RejectInstitutionView(SuperAdminRequiredMixin, View):
    """POST `/platform/institutions/<id>/reject/`, reason required."""

    def post(self, request, pk):
        institution = get_object_or_404(Institution, pk=pk)
        form = RejectInstitutionForm(request.POST)

        if not form.is_valid():
            messages.error(
                request,
                "A reason is required to reject a registration — "
                "it's shown to the applicant.",
            )
            return redirect("platform_admin:institution_list")

        try:
            reject_institution(
                institution,
                reviewer=request.user,
                reason=form.cleaned_data["reason"],
            )
        except ApprovalError as error:
            messages.error(request, str(error))
        except DatabaseError:
            logger.exception("Rejecting institution %s failed", pk)
            messages.error(
                request, "Something went wrong rejecting this school. Please retry."
            )
        else:
            messages.success(request, f"{institution.name} rejected.")
        return redirect("platform_admin:institution_list")
