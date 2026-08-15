"""
Billing forms: fee structures (with an itemized formset), fee adjustments,
payments, and payment reversals.

docs/08_UI_UX.md specifies the fee structure builder uses a Django formset for
itemized lines — "Add another line" submits/reloads with an extra blank row via
the management form, not a JS-built dynamic form. Running total is recalculated
on each page load, not live via JS.
"""

from decimal import Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from academic.models import Class, ClassStatus, Session, Term

from .models import (
    FeeStructure,
    FeeStructureItem,
    Payment,
    PaymentMethod,
    StudentFeeAssignment,
)


class FeeStructureForm(forms.ModelForm):
    """Name, class, session, term. The items come from the formset below."""

    klass = forms.ModelChoiceField(
        queryset=Class.objects.none(),
        label="Class",
        empty_label="Select a class",
    )
    session = forms.ModelChoiceField(
        queryset=Session.objects.none(),
        label="Session",
        empty_label="Select a session",
    )
    term = forms.ModelChoiceField(
        queryset=Term.objects.none(),
        label="Term",
        empty_label="Select a term",
    )

    class Meta:
        model = FeeStructure
        fields = ("name", "klass", "session", "term")

    def __init__(self, *args, institution_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        if institution_id:
            self.fields["klass"].queryset = Class.objects.filter(
                institution_id=institution_id, status=ClassStatus.ACTIVE
            ).order_by("order", "name")
            self.fields["session"].queryset = Session.objects.filter(
                institution_id=institution_id
            ).order_by("-start_date")
            self.fields["term"].queryset = Term.objects.filter(
                institution_id=institution_id
            ).order_by("session__start_date", "start_date")


class FeeStructureItemForm(forms.ModelForm):
    class Meta:
        model = FeeStructureItem
        fields = ("name", "amount")

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is not None and amount <= 0:
            raise forms.ValidationError("Amount must be greater than zero.")
        return amount


class BaseFeeStructureItemFormSet(BaseInlineFormSet):
    """Custom formset validation: at least one item with data."""

    def clean(self):
        super().clean()
        has_items = False
        for form in self.forms:
            if form.cleaned_data and not form.cleaned_data.get("DELETE", False):
                has_items = True
                break
        if not has_items:
            raise forms.ValidationError(
                "A fee structure must have at least one item."
            )


FeeStructureItemFormSet = inlineformset_factory(
    FeeStructure,
    FeeStructureItem,
    form=FeeStructureItemForm,
    formset=BaseFeeStructureItemFormSet,
    fields=("name", "amount"),
    extra=3,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


class StudentFeeAdjustmentForm(forms.Form):
    """Adjust a student's fee assignment amount with a mandatory reason."""

    amount_due = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.00"),
        label="Adjusted amount",
    )
    adjustment_reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Reason for adjustment",
        help_text="Required. This is audit-logged and visible to the institution Owner.",
    )

    def clean_adjustment_reason(self):
        reason = self.cleaned_data.get("adjustment_reason", "").strip()
        if not reason:
            raise forms.ValidationError("A reason is required for fee adjustments.")
        return reason


class PaymentForm(forms.Form):
    """Record a payment against a student fee assignment.

    Supports custom line-item allocations or fast full/lump-sum payment.
    """

    assignment = forms.ModelChoiceField(
        queryset=StudentFeeAssignment.objects.none(),
        label="Student / Fee Assignment",
        empty_label="Select a student and fee",
    )
    amount = forms.DecimalField(
        max_digits=12,
        decimal_places=2,
        min_value=Decimal("0.01"),
        label="Total Amount to Record",
    )
    item_allocations_json = forms.CharField(
        required=False,
        widget=forms.HiddenInput(),
    )
    payment_date = forms.DateField(
        widget=forms.DateInput(attrs={"type": "date"}),
        label="Payment date",
    )
    method = forms.ChoiceField(
        choices=PaymentMethod.choices,
        label="Payment method",
    )
    apply_credit = forms.BooleanField(
        required=False,
        label="Apply existing credit toward this balance first",
        help_text="If the student has credit, it will be applied before the payment.",
    )

    def __init__(self, *args, institution_id=None, **kwargs):
        super().__init__(*args, **kwargs)
        if institution_id:
            self.fields["assignment"].queryset = (
                StudentFeeAssignment.objects.filter(
                    institution_id=institution_id,
                )
                .select_related(
                    "student", "fee_structure", "fee_structure__klass",
                    "fee_structure__term",
                )
                .order_by(
                    "fee_structure__klass__name",
                    "student__last_name",
                    "student__first_name",
                )
            )
            self.fields["assignment"].label_from_instance = lambda obj: (
                f"{obj.student.full_name} ({obj.student.admission_number}) — {obj.fee_structure.name} [{obj.fee_structure.klass.name}] · ₦{obj.outstanding_balance} due"
            )

    def clean_assignment(self):
        assignment = self.cleaned_data.get("assignment")
        if assignment and assignment.outstanding_balance <= 0:
            raise forms.ValidationError(
                "This assignment has no outstanding balance."
            )
        return assignment

    def clean(self):
        cleaned_data = super().clean()
        allocations_raw = cleaned_data.get("item_allocations_json")
        cleaned_allocations = []
        if allocations_raw:
            try:
                import json
                parsed = json.loads(allocations_raw)
                if isinstance(parsed, list):
                    for item in parsed:
                        f_id = item.get("fee_item_id")
                        a_amt = Decimal(str(item.get("amount", "0.00")))
                        if a_amt > 0 and f_id:
                            cleaned_allocations.append({
                                "fee_item_id": int(f_id),
                                "amount": a_amt,
                            })
            except Exception:
                pass
        cleaned_data["cleaned_allocations"] = cleaned_allocations or None
        return cleaned_data


class PaymentReversalForm(forms.Form):
    """Reverse a payment with a mandatory reason."""

    reversal_reason = forms.CharField(
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Reason for reversal",
        help_text="Required. This is audit-logged and cannot be undone.",
    )

    def clean_reversal_reason(self):
        reason = self.cleaned_data.get("reversal_reason", "").strip()
        if not reason:
            raise forms.ValidationError("A reason is required for payment reversals.")
        return reason
