"""
AccessibleFormMixin's contract.

Two halves. The first is the accessibility markup itself, which CLAUDE.md
requires and which templates/includes/field.html depends on by id — the ids in
these assertions and the ones in that template have to stay in step.

The second is the constructor's contract, and it is the reason this file exists:
building a form must not validate it. Reading self.errors during __init__ calls
full_clean() while the form is half-built, before any subclass __init__ further
down the MRO has configured its fields. That is not theoretical — it made every
term submission fail with "Select a valid choice" while TermForm's session
dropdown was still holding the empty queryset Django captured at import time.
"""

from django import forms
from django.test import SimpleTestCase

from core.forms import AccessibleForm


class ExampleForm(AccessibleForm):
    """Stands in for the real forms: a field with help text, one without, and a
    checkbox, which the mixin deliberately leaves alone."""

    name = forms.CharField(help_text="As the school writes it.")
    nickname = forms.CharField(required=False)
    confirmed = forms.BooleanField(help_text="Tick to confirm.")


class LateConfiguringForm(AccessibleForm):
    """A form that narrows a field's choices after super().__init__() returns.

    The shape every scoped form in this project has. If construction validates,
    the submitted value is checked against the choices this __init__ has not
    replaced yet, and a valid submission is rejected.
    """

    choice = forms.ChoiceField(choices=[])

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["choice"].choices = [("a", "A"), ("b", "B")]


class ConstructionDoesNotValidateTests(SimpleTestCase):
    def test_a_bound_form_is_not_validated_until_asked(self):
        form = ExampleForm(data={})
        self.assertIsNone(
            form._errors,
            "Building the form validated it. Every subclass that configures a "
            "field after super().__init__() — which is all the scoped ones — "
            "would then be validated against its unconfigured field.",
        )

    def test_choices_narrowed_after_super_init_are_the_ones_enforced(self):
        """The regression this file was written for, in miniature."""
        form = LateConfiguringForm(data={"choice": "a"})
        self.assertTrue(
            form.is_valid(),
            f"A valid choice was rejected: {form.errors.as_data()}",
        )

    def test_a_value_outside_the_narrowed_choices_is_still_refused(self):
        form = LateConfiguringForm(data={"choice": "z"})
        self.assertFalse(form.is_valid())
        self.assertIn("choice", form.errors)


class AriaMarkupTests(SimpleTestCase):
    def test_help_text_is_linked_on_an_unbound_form(self):
        form = ExampleForm()
        self.assertEqual(
            form.fields["name"].widget.attrs.get("aria-describedby"), "id_name_help"
        )
        self.assertNotIn("aria-invalid", form.fields["name"].widget.attrs)

    def test_a_field_with_no_help_text_gets_no_describedby(self):
        form = ExampleForm()
        self.assertNotIn("aria-describedby", form.fields["nickname"].widget.attrs)

    def test_an_invalid_field_is_marked_and_linked_to_its_error(self):
        form = ExampleForm(data={"name": "", "nickname": "x"})
        self.assertFalse(form.is_valid())

        attrs = form.fields["name"].widget.attrs
        self.assertEqual(attrs.get("aria-invalid"), "true")
        self.assertEqual(attrs.get("aria-describedby"), "id_name_errors id_name_help")

    def test_a_valid_field_on_an_invalid_form_is_not_marked(self):
        form = ExampleForm(data={"name": "", "nickname": "Sunrise"})
        self.assertFalse(form.is_valid())
        self.assertNotIn("aria-invalid", form.fields["nickname"].widget.attrs)

    def test_a_checkbox_is_left_alone_even_when_it_is_the_error(self):
        """aria-invalid on a checkbox is noise — it carries its label inline and
        cannot be malformed, only unticked."""
        form = ExampleForm(data={"name": "Sunrise"})
        self.assertFalse(form.is_valid())
        self.assertIn("confirmed", form.errors)
        self.assertNotIn("aria-invalid", form.fields["confirmed"].widget.attrs)
