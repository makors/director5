import uuid

from django import forms
from django.contrib.auth import get_user_model

from ..users.models import MassEmail


class MassEmailForm(forms.ModelForm):
    request_id = forms.UUIDField(widget=forms.HiddenInput())
    limit_users = forms.ModelMultipleChoiceField(
        queryset=get_user_model().objects.order_by("username"),
        required=False,
        label="Recipients",
        help_text="Leave this empty to email all users with a valid email address.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.fields["request_id"].initial = uuid.uuid4()

    def clean_subject(self):
        subject = self.cleaned_data["subject"]
        if "\r" in subject or "\n" in subject:
            raise forms.ValidationError("Enter the subject on a single line.")
        return subject

    class Meta:
        model = MassEmail
        fields = ["limit_users", "subject", "text_plain", "text_html"]
        labels = {"text_plain": "Plain text message", "text_html": "HTML message"}
        widgets = {
            "text_plain": forms.Textarea(attrs={"rows": 6}),
            "text_html": forms.Textarea(attrs={"rows": 6, "spellcheck": "false"}),
        }


class ConfirmMassEmailForm(forms.Form):
    request_id = forms.UUIDField(widget=forms.HiddenInput())
    confirm_send = forms.BooleanField(
        label="I confirm that this email should be sent to the recipients shown above.",
        required=True,
    )
