"""Forms for site metadata, deployment settings, domains, and members."""

import ipaddress
import re
from urllib.parse import urlsplit

from django import forms
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from .models import Domain, Site

NAME_INPUT = {
    "class": "dt-input",
    "autocomplete": "off",
    "autocapitalize": "none",
    "spellcheck": "false",
}


def validate_site_name_policy(name: str, *, user, purpose: str) -> None:
    """Apply the Director name reservations in addition to model validation."""
    if not name:
        return
    if name[0].isdigit() and purpose != "user" and not user.is_superuser:
        raise ValidationError("Project site names cannot start with a number.")

    try:
        if name not in getattr(settings, "WHITELISTED_SITE_NAMES", ()):
            if name in getattr(settings, "BLACKLISTED_SITE_NAMES", ()) or any(
                re.search(pattern, name)
                for pattern in getattr(settings, "BLACKLISTED_SITE_REGEXES", ())
            ):
                raise ValidationError("This site name is reserved.")
        reserved = getattr(settings, "FORBIDDEN_SITE_NAME_REGEX", "")
        if not user.is_superuser and reserved and re.search(reserved, name):
            raise ValidationError("This site name is reserved for administrators.")
    except re.error as exc:
        raise ValidationError(
            "Site naming policy is unavailable. Contact an administrator."
        ) from exc


class SiteNameForm(forms.ModelForm):
    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if self.instance.purpose == "user":
            self.fields["name"].disabled = True
            self.fields["name"].help_text = "Personal site names match the account username."

    def clean_name(self):
        name = self.cleaned_data["name"]
        if name != self.instance.name:
            if name and name[0].isdigit() and self.instance.purpose != "user":
                raise ValidationError("Project site names cannot start with a number.")
            validate_site_name_policy(name, user=self.user, purpose=self.instance.purpose)
        return name

    class Meta:
        model = Site
        fields = ["name"]
        labels = {"name": "Site name"}
        help_texts = {"name": "Use lowercase letters, numbers, and hyphens."}
        widgets = {"name": forms.TextInput(attrs=NAME_INPUT)}


class SiteMetaForm(forms.ModelForm):
    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        if not user.is_superuser:
            self.fields["purpose"].disabled = True
            self.fields["purpose"].help_text = "Only administrators can change the site purpose."

    def clean_purpose(self):
        purpose = self.cleaned_data["purpose"]
        if purpose != self.instance.purpose:
            validate_site_name_policy(self.instance.name, user=self.user, purpose=purpose)
        return purpose

    class Meta:
        model = Site
        fields = ["description", "purpose"]
        widgets = {
            "description": forms.Textarea(attrs={"class": "dt-input", "rows": 3}),
            "purpose": forms.Select(attrs={"class": "dt-input"}),
        }


class SiteTypeForm(forms.ModelForm):
    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["mode"].choices = Site.TYPES

    class Meta:
        model = Site
        fields = ["mode"]
        labels = {"mode": "Site type"}
        help_texts = {"mode": "Static serves HTML, CSS, and JavaScript. Dynamic runs a container."}
        widgets = {"mode": forms.RadioSelect()}


class SiteAdminForm(forms.ModelForm):
    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)

    class Meta:
        model = Site
        fields = ["admin_comments", "custom_nginx_config"]
        labels = {
            "admin_comments": "Administrator comments",
            "custom_nginx_config": "Custom Nginx configuration",
        }
        help_texts = {
            "admin_comments": "Visible to everyone with access to this site.",
            "custom_nginx_config": "Leave blank to use the generated configuration.",
        }
        widgets = {
            "admin_comments": forms.Textarea(attrs={"class": "dt-input", "rows": 3}),
            "custom_nginx_config": forms.Textarea(
                attrs={"class": "dt-input dt-code-input", "rows": 8, "spellcheck": "false"}
            ),
        }


class AddMemberForm(forms.Form):
    username = forms.CharField(
        label="Username", max_length=32, widget=forms.TextInput(attrs=NAME_INPUT)
    )

    def __init__(self, *args, site, **kwargs):
        super().__init__(*args, **kwargs)
        self.site = site
        self.member = None

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        candidates = list(
            get_user_model().objects.filter(username__iexact=username, is_active=True)[:2]
        )
        if len(candidates) != 1:
            raise ValidationError("This user must sign in to Director before you can add them.")
        self.member = candidates[0]
        if self.site.users.filter(pk=self.member.pk).exists():
            raise ValidationError("This user already has access to the site.")
        return self.member.username


class AddDomainForm(forms.Form):
    domain = forms.CharField(
        label="Domain", max_length=253, widget=forms.TextInput(attrs=NAME_INPUT)
    )

    def __init__(self, *args, site, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.site = site
        self.user = user

    def clean_domain(self):
        domain = self.cleaned_data["domain"].strip().lower().rstrip(".")
        labels = domain.split(".")
        if (
            len(labels) < 2
            or any(len(label) > 63 for label in labels)
            or re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*(\.[a-z0-9]+(-[a-z0-9]+)*)+", domain) is None
        ):
            raise ValidationError("Enter a domain name without a scheme, path, or port.")
        try:
            ipaddress.ip_address(domain)
        except ValueError:
            pass
        else:
            raise ValidationError("Enter a domain name rather than an IP address.")

        generated = settings.SITE_URL_FORMATS[None].format("director-reserved-site")
        generated_host = urlsplit(generated if "://" in generated else "//" + generated).hostname
        generated_zone = generated_host.partition(".")[2] if generated_host else ""
        reserved_zones = {"sites.tjhsst.edu", generated_zone} - {""}
        if any(domain == zone or domain.endswith("." + zone) for zone in reserved_zones):
            raise ValidationError("Use the automatically generated address for sites domains.")
        if not self.user.is_superuser and (
            domain == "tjhsst.edu" or domain.endswith(".tjhsst.edu")
        ):
            raise ValidationError("Only administrators can add tjhsst.edu domains.")

        existing = Domain.objects.filter(domain__iexact=domain).exclude(status="deleted").first()
        if existing:
            if existing.status == "blocked":
                raise ValidationError("This domain is reserved by an administrator.")
            if existing.site_id != self.site.pk:
                raise ValidationError("This domain is assigned to another site.")
            if existing.status != "inactive":
                raise ValidationError("This domain is already assigned to this site.")
        return domain
