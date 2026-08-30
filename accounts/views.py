"""
Auth, registration, and user management views.

Everything here is either public/token-gated or Owner-only staff management.
"""

import logging

from django.contrib import messages
from django.contrib.auth import login, views as auth_views
from django.contrib.auth.tokens import default_token_generator
from django.db import DatabaseError
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.utils.http import urlsafe_base64_decode
from django.views.generic import FormView, ListView, TemplateView

from core.middleware import auth_lookup_context, institution_db_context
from core.mixins import RoleRequiredMixin

from .forms import (
    InstitutionLoginForm,
    InstitutionRegistrationForm,
    UserAcceptInviteForm,
    UserInviteForm,
    UserUpdateForm,
)
from .models import User
from .services import (
    activate_invited_user,
    invite_user,
    register_institution,
    update_user_role_and_status,
)

logger = logging.getLogger(__name__)


class InstitutionRegisterView(FormView):
    """Public self-serve registration (docs/03_Views_and_Endpoints.md)."""

    template_name = "accounts/register.html"
    form_class = InstitutionRegistrationForm
    success_url = reverse_lazy("accounts:register_pending")

    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            return redirect("core:dashboard")
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        data = form.cleaned_data
        try:
            register_institution(
                school_name=data["school_name"],
                school_type=data["school_type"],
                owner_name=data["owner_name"],
                owner_email=data["owner_email"],
                owner_phone=data["owner_phone"],
                password=data["password1"],
            )
        except DatabaseError:
            logger.exception("Institution registration failed")
            form.add_error(
                None,
                "Something went wrong creating your school's account. "
                "Please try again.",
            )
            return self.form_invalid(form)
        return super().form_valid(form)


class RegistrationPendingView(TemplateView):
    template_name = "accounts/register_pending.html"


class InstitutionLoginView(auth_views.LoginView):
    template_name = "accounts/login.html"
    authentication_form = InstitutionLoginForm
    redirect_authenticated_user = False

    def form_valid(self, form):
        with institution_db_context(form.get_user().institution_id):
            return super().form_valid(form)


class InstitutionLogoutView(auth_views.LogoutView):
    next_page = reverse_lazy("accounts:login")


class InstitutionPasswordResetView(auth_views.PasswordResetView):
    template_name = "accounts/password_reset.html"
    email_template_name = "accounts/password_reset_email.txt"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")

    def form_valid(self, form):
        with auth_lookup_context():
            return super().form_valid(form)


class InstitutionPasswordResetDoneView(auth_views.PasswordResetDoneView):
    template_name = "accounts/password_reset_done.html"


class InstitutionPasswordResetConfirmView(auth_views.PasswordResetConfirmView):
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")

    def dispatch(self, request, *args, **kwargs):
        with auth_lookup_context():
            return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form):
        with institution_db_context(self.user.institution_id):
            return super().form_valid(form)


class InstitutionPasswordResetCompleteView(auth_views.PasswordResetCompleteView):
    template_name = "accounts/password_reset_complete.html"


# --------------------------------------------------------------------------- #
# Staff User Management Views (Owner Only, module "users")
# --------------------------------------------------------------------------- #
class UserListView(RoleRequiredMixin, ListView):
    """`/settings/users/` — Owner only."""

    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "staff_members"
    module = "users"
    paginate_by = 50

    def get_queryset(self):
        return User.objects.filter(
            institution_id=self.request.institution_id
        ).order_by("role", "full_name")


class UserInviteView(RoleRequiredMixin, FormView):
    """`/settings/users/invite/` — Owner invites staff, generating setup link (CR-003)."""

    template_name = "accounts/user_invite.html"
    form_class = UserInviteForm
    module = "users"
    module_action = "manage"

    def form_valid(self, form):
        try:
            user, uidb64, token = invite_user(
                institution_id=self.request.institution_id,
                full_name=form.cleaned_data["full_name"],
                email=form.cleaned_data["email"],
                role=form.cleaned_data["role"],
                actor=self.request.user,
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
            accept_url = self.request.build_absolute_uri(
                reverse_lazy(
                    "accounts:user_accept_invite",
                    kwargs={"uidb64": uidb64, "token": token},
                )
            )
            messages.success(
                self.request,
                f"Invitation link created for {user.full_name} ({user.role}).",
            )
            return self.render_to_response(
                self.get_context_data(
                    form=form,
                    setup_link=accept_url,
                    invited_user=user,
                )
            )
        except Exception:
            logger.exception("Failed to invite user")
            form.add_error(None, "Something went wrong generating the invite.")
            return self.form_invalid(form)


class UserAcceptInviteView(FormView):
    """`/settings/users/accept/<uidb64>/<token>/` — Public setup page for invited staff."""

    template_name = "accounts/user_accept_invite.html"
    form_class = UserAcceptInviteForm

    def dispatch(self, request, *args, **kwargs):
        self.target_user = self._get_user(kwargs.get("uidb64"), kwargs.get("token"))
        if not self.target_user:
            messages.error(request, "This invitation link is invalid or has expired.")
            return redirect("accounts:login")
        return super().dispatch(request, *args, **kwargs)

    def _get_user(self, uidb64, token):
        try:
            with auth_lookup_context():
                uid = urlsafe_base64_decode(uidb64).decode()
                user = User.objects.get(pk=uid)
                if not user.is_active and default_token_generator.check_token(user, token):
                    return user
        except Exception:
            pass
        return None

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["user"] = self.target_user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["target_user"] = self.target_user
        return context

    def form_valid(self, form):
        password = form.cleaned_data["password1"]
        user = activate_invited_user(
            user=self.target_user,
            password=password,
            ip_address=self.request.META.get("REMOTE_ADDR"),
        )
        with institution_db_context(user.institution_id):
            login(self.request, user, backend="accounts.backends.InstitutionUserBackend")
        messages.success(self.request, f"Welcome to AdminPilot, {user.full_name}!")
        return redirect("core:dashboard")


class UserUpdateView(RoleRequiredMixin, FormView):
    """`/settings/users/<id>/edit/` — Owner edits staff role/status."""

    template_name = "accounts/user_form.html"
    form_class = UserUpdateForm
    module = "users"
    module_action = "manage"

    def dispatch(self, request, *args, **kwargs):
        self.target_user = get_object_or_404(
            User.objects.filter(institution_id=request.institution_id),
            pk=kwargs["pk"],
        )
        return super().dispatch(request, *args, **kwargs)

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["instance"] = self.target_user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["target_user"] = self.target_user
        return context

    def form_valid(self, form):
        try:
            update_user_role_and_status(
                user=self.target_user,
                role=form.cleaned_data["role"],
                is_active=form.cleaned_data["is_active"],
                actor=self.request.user,
                ip_address=self.request.META.get("REMOTE_ADDR"),
            )
            messages.success(
                self.request,
                f"Updated {self.target_user.full_name}'s account.",
            )
            return redirect("accounts:user_list")
        except Exception:
            logger.exception("Failed to update user")
            form.add_error(None, "Something went wrong updating the user.")
            return self.form_invalid(form)
