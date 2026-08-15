"""
Shared form behaviour.

AccessibleFormMixin exists because templates/includes/field.html renders
`{{ field }}` as Django built it, and Django does not mark invalid widgets or
link them to their own error and help text. CLAUDE.md requires generated code
to be accessibility compliant, and every screen in this product is a form or a
table — doing it per-form would mean doing it inconsistently.
"""

import re
import zoneinfo

from django import forms

from core.models import Institution

# Every zone the runtime knows, rather than a hand-picked shortlist. A curated
# list would be a scope decision (which countries do we serve?) that no document
# makes, and it would be wrong the moment a school outside the list registers.
# zoneinfo ships with Python, so this costs no dependency.
TIMEZONE_CHOICES = tuple(
    (name, name.replace("_", " ")) for name in sorted(zoneinfo.available_timezones())
)

CODE_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9-]*[A-Z0-9]$")


def timezone_field(**kwargs):
    """The institution timezone control, shared by the wizard and settings.

    Both screens edit the same column and must offer the same values; two
    separate field definitions would eventually disagree.
    """
    kwargs.setdefault("label", "Timezone")
    kwargs.setdefault(
        "help_text",
        "Used for dates, receipt timestamps, and the dashboard greeting.",
    )
    return forms.ChoiceField(choices=TIMEZONE_CHOICES, **kwargs)


class AccessibleFormMixin:
    """Wire aria-invalid and aria-describedby onto every widget.

    The ids match the ones field.html emits (`<auto_id>_errors`,
    `<auto_id>_help`). Change one and the other has to follow.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Help text only, at this point: self._errors is still None and reading
        # self.errors here would be a bug — see full_clean below.
        self._apply_aria_attributes()

    def full_clean(self):
        """Re-wire once the errors are known.

        The aria-invalid half has to wait for validation, and — the part that
        bit — it must not *cause* validation. This used to read self.errors
        from __init__, which calls full_clean() while the form is still being
        constructed, before any subclass __init__ below this one in the MRO has
        configured its fields.

        TermForm is the case that failed. Its session dropdown is restricted to
        one institution on the line after super().__init__(), so validating
        early checked the submitted session against the queryset
        ForeignKey.formfield() captured when the class was created: that is
        Session._default_manager, the tenant-scoped one, evaluated at import
        with no institution stamped. TenantScopedManager fails closed to
        .none() there and the queryset stays empty for the life of the process,
        so every term — including a perfectly valid one — came back "Select a
        valid choice."

        full_clean() runs on first access to errors/is_valid(), which is still
        before anything renders, so a re-rendered bound form is marked up the
        same as it always was.
        """
        super().full_clean()
        self._apply_aria_attributes()

    def _apply_aria_attributes(self):
        # self._errors is None until full_clean() has run. Read it directly
        # rather than through the errors property, which would trigger the
        # validation this method is called from.
        errors = self._errors or {}
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, (forms.CheckboxInput, forms.RadioSelect)):
                # Checkboxes carry their label inline; aria-invalid on them is
                # noise, since a checkbox cannot be malformed.
                continue

            described_by = []
            auto_id = self.auto_id % name if self.auto_id and "%s" in self.auto_id else name

            if name in errors:
                widget.attrs["aria-invalid"] = "true"
                described_by.append(f"{auto_id}_errors")
            if field.help_text:
                described_by.append(f"{auto_id}_help")

            if described_by:
                widget.attrs["aria-describedby"] = " ".join(described_by)


class AccessibleForm(AccessibleFormMixin, forms.Form):
    pass


class AccessibleModelForm(AccessibleFormMixin, forms.ModelForm):
    pass


class InstitutionDetailsForm(AccessibleModelForm):
    """Institution details, shared by the setup wizard and Institution Settings.

    The `code` lock lives here rather than in either view, because both edit the
    same column and the rule is a property of the column, not of the screen.
    docs/03_Views_and_Endpoints.md is specific about the failure mode: an inline
    form error, not a 403 and not a silently discarded value.
    """

    timezone = timezone_field()

    class Meta:
        model = Institution
        fields = (
            "name",
            "code",
            "type",
            "timezone",
            "email",
            "phone",
            "address",
        )
        labels = {
            "name": "School name",
            "code": "Institution code",
            "type": "School type",
            "email": "Contact email",
            "phone": "Contact phone",
        }
        help_texts = {
            "code": (
                "Prefix for every admission and receipt number, e.g. "
                "PERM-2026-000123. Fixed once the first receipt is issued."
            ),
        }
        widgets = {
            "address": forms.Textarea(attrs={"rows": 3}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs["autofocus"] = True
        if self.code_is_locked():
            # Disabled means Django ignores the submitted value entirely and
            # reuses the instance's, so a crafted POST cannot set it either. The
            # visible explanation is the help text below; belt and braces.
            self.fields["code"].disabled = True
            self.fields["code"].help_text = (
                "Fixed: receipts have already been issued under this code, and "
                "changing it now would leave those receipt numbers pointing at a "
                "code this school no longer uses."
            )

    def code_is_locked(self):
        """True once any receipt exists for this institution.

        Imported inside the method on purpose. `core` is the app nothing else's
        models depend on (docs/01_Architecture.md ADR-003); a module-level
        `from billing.models import Receipt` here would invert that and make the
        dependency graph a cycle.
        """
        if self.instance.pk is None:
            return False
        from billing.models import Receipt

        return Receipt.unscoped.filter(institution_id=self.instance.pk).exists()

    def clean_code(self):
        code = (self.cleaned_data.get("code") or "").strip().upper()
        if not code:
            return code
        if not CODE_PATTERN.match(code):
            raise forms.ValidationError(
                "Use letters, numbers and hyphens only — it becomes part of "
                "every receipt number."
            )
        # The disabled field above already blocks this, so reaching here means
        # the lock was bypassed some other way. Answering with the documented
        # inline error is still the right response.
        if self.code_is_locked() and code != self.instance.code:
            raise forms.ValidationError(
                "This code can no longer be changed: receipts have already been "
                "issued under it."
            )
        return code

    def clean_name(self):
        return self.cleaned_data["name"].strip()


class InstitutionSettingsForm(InstitutionDetailsForm):
    """`/settings/institution/`. Identical fields to the wizard's first step.

    A separate class only so the two screens can diverge later without one
    silently inheriting the other's change.
    """
