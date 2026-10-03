import re

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.template.loader import render_to_string

from .models import Site


class DirectorSelect(forms.Select):
    def render(self, name, value, attrs=None, renderer=None):
        return render_to_string(
            "components/dropdown.html",
            {
                "extra_div_classes": self.attrs.get("extra_div_classes", ""),
                "extra_input_classes": self.attrs.get("extra_input_classes", ""),
                "extra_li_classes": self.attrs.get("extra_li_classes", ""),
                "elem_name": name,
                "elem_id": self.attrs.get("id", "id_" + name),
                "choices": self.choices,
            },
        )


class CreateSiteForm(forms.ModelForm):
    """The :class:`forms.ModelForm` for creating a website (static/dynamic).

    Please note that there is logic in create_form.html that relies directly on the form names and model values.
    If you change anything here, please ensure that creating a site using the form still works.
    """

    PURPOSES = (
        ("project", "Project"),
        ("user", "User"),
        ("activity", "Activity"),
        ("other", "Other"),
    )

    student_agreement = forms.BooleanField(
        required=True,
        label="I agree to the website guidelines",
        help_text="Read the Guidelines before creating a site.",
    )

    def __init__(self, *args, user=None, personal=False, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        from .governance_forms import student_agreement_help_text

        self.fields["student_agreement"].help_text = student_agreement_help_text()
        self.fields["purpose"].choices = CreateSiteForm.PURPOSES
        self.fields["mode"].choices = Site.TYPES
        self.fields["purpose"].initial = "project"
        self.fields["purpose"].help_text = ""
        self.fields["mode"].initial = "static"
        self.fields["name"].help_text = "Use lowercase letters, numbers, and hyphens."
        self.fields["name"].label = "Site name"
        self.fields["mode"].label = "Site type"
        self.fields["mode"].help_text = "Static hosts web files. Dynamic runs a server application."
        self.fields["users"].queryset = (
            get_user_model().objects.filter(is_active=True, is_service=False).order_by("username")
        )
        self.fields["users"].required = False
        self.fields["users"].label = "Additional people with access"
        self.fields[
            "users"
        ].help_text = "You will have access automatically. You can also add people in Settings."
        if user and not user.is_superuser:
            self.fields["purpose"].initial = "user" if personal else "project"
            self.fields["purpose"].disabled = True
        if personal and user:
            self.fields["purpose"].initial = "user"
            self.fields["purpose"].disabled = True
            self.fields["name"].initial = user.username
            self.fields["name"].disabled = True
            self.fields["users"].disabled = True

    def clean_name(self):
        name = self.cleaned_data["name"]
        if self.user and not self.user.is_superuser:
            if self.fields["purpose"].initial != "user" and name[0].isdigit():
                raise forms.ValidationError("Project site names cannot start with a number.")
            try:
                reserved = getattr(settings, "FORBIDDEN_SITE_NAME_REGEX", "")
                if reserved and re.search(reserved, name):
                    raise forms.ValidationError("This site name is reserved for administrators.")
            except re.error as exc:
                raise forms.ValidationError(
                    "Site name validation is misconfigured. Contact an administrator."
                ) from exc
        return name

    class Meta:
        model = Site
        fields = ["name", "description", "mode", "purpose", "users"]
        widgets = {
            "name": forms.TextInput(attrs={"class": "dt-input block"}),
            "description": forms.Textarea(attrs={"class": "dt-input block", "rows": 3}),
            "purpose": forms.Select(attrs={"class": "dt-input block"}),
            "mode": forms.RadioSelect(),
            "users": forms.SelectMultiple(attrs={"class": "dt-input block", "size": 3}),
        }
