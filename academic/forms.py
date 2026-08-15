"""
Forms for the academic structure: Sessions, Terms and Classes.

Every form here takes `institution_id` explicitly rather than reading it off a
request. The uniqueness rules are per-institution, and a ModelForm that excludes
the `institution` field also skips Django's own check on any constraint that
mentions it — so the checks are written out below, with the institution passed in
by the view that already resolved it.
"""

from django import forms

from core.forms import AccessibleFormMixin, AccessibleModelForm
from core.models import TenantScopedModel

from .models import Class, ClassStatus, Session, Term


class InstitutionScopedFormMixin:
    """Accepts the institution the form is being filled in for.

    Not optional and not defaulted: a uniqueness check that silently ran against
    every institution, or against none, would be worse than a TypeError here.
    """

    def __init__(self, *args, institution_id, **kwargs):
        self.institution_id = institution_id
        super().__init__(*args, **kwargs)
        self._scope_relation_fields()

    def _scope_relation_fields(self):
        """Point every relation dropdown at this institution's rows.

        ForeignKey.formfield() builds its queryset from the related model's
        _default_manager exactly once, when the form class is created. For a
        TenantScopedModel that manager is the tenant-scoped one, and at import
        time no institution is stamped — so it fails closed to .none() and stays
        empty for the life of the process. A relation field left as Django built
        it therefore rejects every choice, the correct one included.

        Doing it here rather than in each form means a new form with a foreign
        key to a tenant-scoped model is scoped by default instead of being
        quietly broken until someone submits it. `unscoped` plus an explicit
        filter, because forms are also built in tests and management commands
        where nothing is stamped; RLS is still underneath either way.
        """
        for field in self.fields.values():
            model = getattr(getattr(field, "queryset", None), "model", None)
            if model is not None and issubclass(model, TenantScopedModel):
                field.queryset = model.unscoped.filter(
                    institution_id=self.institution_id
                )


class DateRangeMixin:
    """start_date/end_date ordering, shared by Session and Term."""

    def clean(self):
        cleaned = super().clean()
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")
        if start and end and end <= start:
            self.add_error("end_date", "The end date must fall after the start date.")
        return cleaned


class SessionForm(InstitutionScopedFormMixin, DateRangeMixin, AccessibleModelForm):
    """Create or edit an academic session, e.g. "2025/2026"."""

    class Meta:
        model = Session
        fields = ("name", "start_date", "end_date")
        labels = {"name": "Session name"}
        help_texts = {"name": 'How your school writes it, e.g. "2025/2026".'}
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs["autofocus"] = True

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        duplicate = Session.unscoped.filter(
            institution_id=self.institution_id, name__iexact=name
        ).exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError("You already have a session with this name.")
        return name


class TermForm(InstitutionScopedFormMixin, DateRangeMixin, AccessibleModelForm):
    """Create or edit a term inside a session.

    The session dropdown is re-queried per instantiation and restricted to this
    institution by InstitutionScopedFormMixin — see _scope_relation_fields for
    why the queryset Django builds for a tenant-scoped foreign key cannot be
    used as-is.
    """

    class Meta:
        model = Term
        fields = ("session", "name", "start_date", "end_date")
        labels = {"name": "Term name"}
        help_texts = {"name": 'For example "First Term".'}
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
        }

    def clean(self):
        cleaned = super().clean()
        session = cleaned.get("session")
        start = cleaned.get("start_date")
        end = cleaned.get("end_date")

        if session and start and not session.start_date <= start <= session.end_date:
            self.add_error(
                "start_date",
                f"This term starts outside its session, which runs "
                f"{session.start_date} to {session.end_date}.",
            )
        if session and end and not session.start_date <= end <= session.end_date:
            self.add_error(
                "end_date",
                f"This term ends outside its session, which runs "
                f"{session.start_date} to {session.end_date}.",
            )

        name = cleaned.get("name")
        if session and name:
            duplicate = Term.unscoped.filter(
                institution_id=self.institution_id,
                session=session,
                name__iexact=name.strip(),
            ).exclude(pk=self.instance.pk)
            if duplicate.exists():
                self.add_error("name", "This session already has a term by that name.")
        return cleaned


class ClassForm(InstitutionScopedFormMixin, AccessibleModelForm):
    """Create or edit one class.

    `status` is editable here as well as through the deactivate button, because
    docs/03_Views_and_Endpoints.md lists it among ClassUpdateView's fields. Both
    paths are safe: deactivating a class blocks new enrollments and leaves the
    students already in it alone.
    """

    class Meta:
        model = Class
        fields = ("name", "order", "status")
        labels = {"name": "Class name", "order": "Display order"}
        help_texts = {
            "name": (
                "Must match the worksheet tab name used for bulk import, so "
                'write it the way your staff write it — e.g. "JSS 1A".'
            ),
            "order": "Lower numbers appear first in lists and dropdowns.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["name"].widget.attrs["autofocus"] = True

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        # uniq_class_name_per_inst covers this in the database, but the database
        # answers with an IntegrityError, and CLAUDE.md forbids handing a raw
        # database error back. Checking here turns it into a field error.
        duplicate = Class.unscoped.filter(
            institution_id=self.institution_id, name__iexact=name
        ).exclude(pk=self.instance.pk)
        if duplicate.exists():
            raise forms.ValidationError(
                "You already have a class with this name. Class names are used as "
                "the import worksheet tab names, so they have to stay unique."
            )
        return name


class SetupClassesForm(InstitutionScopedFormMixin, AccessibleFormMixin, forms.Form):
    """Wizard step 3: the initial class list, one name per line.

    A textarea rather than a formset. The wizard's job is to get a school from
    zero classes to a usable list in one screen, and a school typing in fifteen
    class names should not need fifteen "add another row" round trips —
    docs/08_UI_UX.md's minimal-JS rule rules out doing it client-side. Ongoing
    single-class edits go through ClassForm.
    """

    names = forms.CharField(
        label="Class names",
        widget=forms.Textarea(
            attrs={
                "rows": 10,
                "autofocus": True,
                "placeholder": "JSS 1A\nJSS 1B\nJSS 2A",
            }
        ),
        help_text=(
            "One class per line, in the order you want them listed. You can add, "
            "rename, and deactivate classes later."
        ),
    )

    def clean_names(self):
        raw = self.cleaned_data["names"]
        names = []
        seen = set()
        for line in raw.splitlines():
            name = line.strip()
            if not name:
                continue
            if len(name) > 100:
                raise forms.ValidationError(
                    f"“{name[:40]}…” is too long for a class name (100 characters "
                    f"maximum)."
                )
            key = name.casefold()
            if key in seen:
                raise forms.ValidationError(f"“{name}” is listed more than once.")
            seen.add(key)
            names.append(name)

        if not names:
            raise forms.ValidationError("Enter at least one class name.")

        existing = {
            name.casefold()
            for name in Class.unscoped.filter(
                institution_id=self.institution_id
            ).values_list("name", flat=True)
        }
        clashes = [name for name in names if name.casefold() in existing]
        if clashes:
            raise forms.ValidationError(
                "You already have a class named " + ", ".join(f"“{c}”" for c in clashes)
            )
        return names


class ClassDeactivationForm(forms.Form):
    """Nothing to fill in — this exists so the POST-only deactivate view has a
    CSRF-protected form object and a place for a future confirmation reason."""

    def deactivate(self, instance):
        instance.status = ClassStatus.INACTIVE
        instance.save(update_fields=["status"])
        return instance
