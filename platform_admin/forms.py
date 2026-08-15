"""
Super Admin forms.

Separate from accounts/forms.py on purpose (CR-004): "no shared code path from
Super Admin into tenant data" should hold at the form layer too, not only at
the URL. Nothing here imports a tenant-scoped model.
"""

from django import forms
from django.contrib.auth.forms import AuthenticationForm

from core.forms import AccessibleFormMixin

from .models import PlatformUser


class PlatformLoginForm(AccessibleFormMixin, AuthenticationForm):
    """`/platform/login/`.

    No institution status check, because a PlatformUser has no institution.
    That absence is the separation, not an omission.
    """

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )

    def confirm_login_allowed(self, user):
        # authenticate() tries both backends, so an institution user's email
        # typed here can come back as accounts.User. They must not receive a
        # platform session — this is the mirror of the same check in
        # InstitutionLoginForm.
        if not isinstance(user, PlatformUser):
            raise forms.ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": self.username_field.verbose_name},
            )
        if not user.is_active:
            raise forms.ValidationError("This account is not active.", code="inactive")


class RejectInstitutionForm(AccessibleFormMixin, forms.Form):
    """Reason is required — docs/03_Views_and_Endpoints.md and 08_UI_UX.md.

    It is shown to the applicant, so it is a message, not an internal note.
    """

    reason = forms.CharField(
        label="Reason for rejection",
        widget=forms.Textarea(attrs={"rows": 3}),
        max_length=1000,
        help_text="Shown to the applicant. Be specific enough to act on.",
    )
