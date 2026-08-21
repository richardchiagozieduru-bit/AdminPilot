"""
View mixins: role gating and tenant-scoped querysets.

docs/03_Views_and_Endpoints.md's conventions section requires both — role
checks referencing the permission matrix rather than per-view `if` statements,
and every CBV touching a tenant-scoped model filtering through one mixin.
"""

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ImproperlyConfigured, PermissionDenied
from django.shortcuts import redirect

from core.permissions import has_module_access


class InstitutionUserRequiredMixin:
    """Authenticated, institution-scoped, and inside an approved institution.

    Not Django's LoginRequiredMixin: this also has to turn away a PlatformUser.
    A Super Admin holds a valid session, so `is_authenticated` is True for them
    on every institution URL — without this check they would fall through to
    the role check, which reads a `role` attribute they do not have.

    The institution status re-check is not redundant with LoginView. A session
    outlives the check that created it: an institution suspended while its
    Owner is logged in must lose access on the next request, not at the next
    login.

    The checks live in `check_access` rather than inline in dispatch so a
    subclass can add its own *before* the view body runs. Chaining through
    dispatch instead would mean calling super().dispatch() — which executes the
    view — and only then deciding whether the caller was allowed to.
    """

    def check_access(self, request):
        """Return a response to short-circuit with, or None to let the view run."""
        user = request.user

        if not user.is_authenticated:
            return redirect_to_login(request.get_full_path())

        # PlatformUser has no institution_id column at all — that structural
        # absence (docs/02_Database.md) is what this reads.
        if getattr(user, "institution_id", None) is None:
            return redirect("platform_admin:institution_list")

        if not user.institution.is_active_tenant:
            from django.contrib.auth import logout

            logout(request)
            messages.error(
                request,
                "Your school's account is no longer active. "
                "Please contact AdminPilot support.",
            )
            return redirect("accounts:login")

        return None

    def dispatch(self, request, *args, **kwargs):
        blocked = self.check_access(request)
        if blocked is not None:
            return blocked
        return super().dispatch(request, *args, **kwargs)


class RoleRequiredMixin(InstitutionUserRequiredMixin):
    """Gate a view on one module of docs/04_Permission_Matrix.md.

        class StudentCreateView(RoleRequiredMixin, CreateView):
            module = "students"
            module_action = "manage"

    The view names the module; the matrix decides the roles. A view listing
    roles itself would be the scattered `if` check the doc rules out.
    """

    module = None
    module_action = "view"

    def check_access(self, request):
        if self.module is None:
            raise ImproperlyConfigured(
                f"{type(self).__name__} uses RoleRequiredMixin but sets no "
                f"`module`. Name a key from core.permissions.MODULE_ACCESS."
            )

        # Authentication first, so an anonymous request gets a login redirect
        # rather than a 403 that tells them the URL exists.
        blocked = super().check_access(request)
        if blocked is not None:
            return blocked

        # Before the view body, not after. A `manage` check that ran on the way
        # out would let an unauthorised POST create the row and then discard the
        # response it produced — the 403 would be honest and the write would
        # still be there.
        if not has_module_access(request.user, self.module, self.module_action):
            raise PermissionDenied(
                f"Your role does not have {self.module_action} access to "
                f"{self.module}."
            )
        return None

    def can_manage(self):
        """For templates deciding whether to draw edit controls.

        Same matrix, same module, so a read-only screen cannot disagree with the
        view that would reject the write.
        """
        return has_module_access(self.request.user, self.module, "manage")


class TenantScopedQuerysetMixin:
    """Filter a CBV's queryset to the request's institution.

    The database Security Policy is the actual floor (docs/01_Architecture.md
    ADR-001); this is the second layer, and its real value is that a bug shows
    up as a visibly empty page in development rather than as silent reliance on
    RLS. The model's default manager is already scoped, so this is mostly a
    belt-and-braces assertion that the view did not reach for `unscoped`.
    """

    def get_queryset(self):
        queryset = super().get_queryset()
        institution_id = getattr(self.request, "institution_id", None)
        if institution_id is None:
            return queryset.none()
        return queryset.filter(institution_id=institution_id)


class AuditedFormMixin:
    """Write an AuditLog row in the same transaction as the form's save.

    docs/02_Database.md: audit entries are written server-side, inside the
    mutation's transaction, never reported by a client. Subclasses supply the
    action verb and the human-readable summary.
    """

    audit_action = None

    def audit_summary(self, obj):
        raise NotImplementedError

    def log_audit(self, obj, action=None, reason="", detail=None):
        from core.services import write_audit_log

        write_audit_log(
            institution_id=self.request.institution_id,
            actor=self.request.user,
            action=action or self.audit_action,
            summary=self.audit_summary(obj),
            target_type=type(obj).__name__,
            target_id=str(obj.pk),
            reason=reason,
            detail=detail,
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
