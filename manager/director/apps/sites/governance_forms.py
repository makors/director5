from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils.html import format_html
from django.utils.safestring import mark_safe

from .models import SiteRequest


def student_agreement_help_text():
    configured = getattr(settings, "DIRECTOR_SITE_STUDENT_AGREEMENT_HELP_TEXT", "")
    if configured:
        return mark_safe(configured)
    return format_html(
        "I have read, understood, and agree to abide by the rules outlined in the "
        "Computer Systems Lab Policy, the <a href='{}' class='underline'>TJHSST Website "
        "Guidelines</a>, the <a href='https://www.fcps.edu/about-fcps/"
        "policies-regulations-and-notices/student-rights-and-responsibilities/appendices"
        "#appendix-a-acceptable-use-policy-for-student-network-access' class='underline'>"
        "FCPS Acceptable Use Policy for Student Network Access</a>, "
        "and the <a href='https://www.fcps.edu/srr' class='underline'>FCPS Student Rights "
        "and Responsibilities</a>. I understand that the above services may be revoked "
        "at any time and other disciplinary actions may occur if I directly or indirectly "
        "violate any guidelines as outlined in the above policies.",
        reverse("sites:guidelines-read"),
    )


class TeacherChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        name = obj.full_name.strip()
        return f"{name} ({obj.username})" if name else obj.username


class SiteRequestForm(forms.ModelForm):
    teacher = TeacherChoiceField(
        queryset=get_user_model()
        .objects.filter(is_teacher=True, is_active=True)
        .order_by("last_name", "first_name", "username"),
        help_text=(
            "Teachers must sign into Director before they are listed here. If you do not "
            "see your activity's sponsor, ask them to sign in first."
        ),
    )
    student_agreement = forms.BooleanField(required=True, label="Student agreement")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["student_agreement"].help_text = student_agreement_help_text()

    class Meta:
        model = SiteRequest
        fields = ["activity", "extra_information", "teacher"]
        labels = {"extra_information": "Additional information"}
        help_texts = {
            "activity": "The name of the activity on behalf of which you are requesting the site.",
            "extra_information": (
                "Please enter any additional information you want the teacher or the Sysadmins "
                "to know."
            ),
        }
        widgets = {
            "activity": forms.TextInput(attrs={"autocomplete": "off"}),
            "extra_information": forms.Textarea(attrs={"rows": 3}),
        }


class TeacherReviewForm(forms.Form):
    action = forms.ChoiceField(choices=[("accept", "Approve"), ("reject", "Reject")])
    agreement = forms.BooleanField(required=False, label="Teacher agreement")

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("action") == "accept" and not cleaned.get("agreement"):
            self.add_error("agreement", "Accept the teacher agreement to approve this request.")
        return cleaned


class AdminReviewForm(forms.Form):
    action = forms.ChoiceField(choices=[("accept", "Approve"), ("reject", "Reject")])
    admin_comments = forms.CharField(
        required=False,
        label="Comments for the student and teacher",
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    private_admin_comments = forms.CharField(
        required=False,
        label="Private administrator comments",
        help_text="Visible only to administrators.",
        widget=forms.Textarea(attrs={"rows": 2}),
    )


class GuidelinesForm(forms.Form):
    accepted = forms.BooleanField(
        required=True, label="I have read and agree to the TJHSST Website Guidelines."
    )
