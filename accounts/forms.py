"""
Registration, login, user invitation, setup accept, and user edit forms.

The login form carries most of the interesting behaviour: docs/08_UI_UX.md
makes a deliberate exception to normal auth-error vagueness for the
pending-institution case, and that exception has to be implemented somewhere
that can tell the two situations apart.
"""

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import AuthenticationForm

from core.forms import AccessibleFormMixin
from core.middleware import auth_lookup_context
from core.models import Institution

from .models import User


class InstitutionRegistrationForm(AccessibleFormMixin, forms.Form):
    """The public registration form."""

    school_name = forms.CharField(
        label="School name",
        max_length=255,
        widget=forms.TextInput(attrs={"autofocus": True, "autocomplete": "organization"}),
    )
    school_type = forms.ChoiceField(
        label="School type",
        choices=Institution.Type.choices,
        initial=Institution.Type.COMBINED,
    )
    owner_name = forms.CharField(
        label="Your full name",
        max_length=255,
        widget=forms.TextInput(attrs={"autocomplete": "name"}),
    )
    owner_email = forms.EmailField(
        label="Your email",
        help_text="You will sign in with this address once your school is approved.",
        widget=forms.EmailInput(attrs={"autocomplete": "email"}),
    )
    owner_phone = forms.CharField(
        label="Your phone number",
        max_length=32,
        required=False,
        widget=forms.TextInput(attrs={"autocomplete": "tel"}),
    )
    password1 = forms.CharField(
        label="Password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def clean_owner_email(self):
        email = self.cleaned_data["owner_email"]
        with auth_lookup_context():
            if User.objects.filter(email__iexact=email).exists():
                raise forms.ValidationError(
                    "An account with this email already exists. "
                    "If your school has already registered, sign in instead."
                )
        return email

    def clean_school_name(self):
        name = self.cleaned_data["school_name"].strip()
        return name

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two passwords do not match.")
        elif password1:
            candidate = User(
                email=cleaned.get("owner_email") or "",
                full_name=cleaned.get("owner_name") or "",
            )
            try:
                password_validation.validate_password(password1, candidate)
            except forms.ValidationError as error:
                self.add_error("password1", error)
        return cleaned


class InstitutionLoginForm(AccessibleFormMixin, AuthenticationForm):
    """Login for institution users."""

    username = forms.EmailField(
        label="Email",
        widget=forms.EmailInput(attrs={"autofocus": True, "autocomplete": "email"}),
    )

    STATUS_MESSAGES = {
        Institution.Status.PENDING: (
            "Your school's registration is still under review. "
            "We'll email you as soon as it's approved."
        ),
        Institution.Status.REJECTED: (
            "Your school's registration was not approved. "
            "Please contact AdminPilot support if you think this is a mistake."
        ),
        Institution.Status.SUSPENDED: (
            "Your school's account is currently suspended. "
            "Please contact AdminPilot support."
        ),
    }

    def confirm_login_allowed(self, user):
        if not isinstance(user, User):
            raise forms.ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": self.username_field.verbose_name},
            )

        institution = user.institution
        if institution.status != Institution.Status.APPROVED:
            raise forms.ValidationError(
                self.STATUS_MESSAGES.get(
                    institution.status,
                    "Your school's account is not active. "
                    "Please contact AdminPilot support.",
                ),
                code="institution_not_approved",
            )

        if not user.is_active:
            raise forms.ValidationError(
                "This account is not active. Please contact your school's "
                "AdminPilot Owner.",
                code="inactive",
            )


class UserInviteForm(AccessibleFormMixin, forms.Form):
    """Owner invites a new staff account (Administrator or Bursar). CR-003."""

    ROLE_CHOICES = (
        (User.Role.ADMINISTRATOR, "Administrator"),
        (User.Role.BURSAR, "Bursar"),
    )

    full_name = forms.CharField(
        label="Full name",
        max_length=255,
        widget=forms.TextInput(attrs={"autofocus": True}),
    )
    email = forms.EmailField(
        label="Email address",
        help_text="The staff member will use this email to sign in.",
    )
    role = forms.ChoiceField(
        label="Role",
        choices=ROLE_CHOICES,
        help_text="Administrator has full management access; Bursar has financial access.",
    )

    def clean_email(self):
        email = self.cleaned_data["email"].strip()
        with auth_lookup_context():
            if User.objects.filter(email__iexact=email).exists():
                raise forms.ValidationError(
                    "An account with this email address already exists."
                )
        return email


class UserAcceptInviteForm(AccessibleFormMixin, forms.Form):
    """Password creation form for invited staff accepting setup link."""

    password1 = forms.CharField(
        label="Create password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label="Confirm password",
        strip=False,
        widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
    )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")

        if password1 and password2 and password1 != password2:
            self.add_error("password2", "The two passwords do not match.")
        elif password1 and self.user:
            try:
                password_validation.validate_password(password1, self.user)
            except forms.ValidationError as error:
                self.add_error("password1", error)
        return cleaned


class UserUpdateForm(AccessibleFormMixin, forms.ModelForm):
    """Owner edits a staff member's role or active status."""

    ROLE_CHOICES = (
        (User.Role.ADMINISTRATOR, "Administrator"),
        (User.Role.BURSAR, "Bursar"),
    )

    role = forms.ChoiceField(
        label="Role",
        choices=ROLE_CHOICES,
    )
    is_active = forms.BooleanField(
        label="Account Active",
        required=False,
        help_text="Uncheck to disable this staff member's access.",
    )

    class Meta:
        model = User
        fields = ("role", "is_active")

    def clean_role(self):
        role = self.cleaned_data.get("role")
        if self.instance.is_owner and role != User.Role.OWNER:
            raise forms.ValidationError("The Owner's role cannot be changed.")
        return role
