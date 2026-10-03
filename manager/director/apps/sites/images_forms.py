"""Validated image customization and administrator resource forms."""

import math
import re

from django import forms
from django.conf import settings

from .models import DockerImage, DockerImageSetupCommand, SiteResourceLimits

PACKAGE_RE = re.compile(r"^[a-zA-Z0-9_][-+=_.a-zA-Z0-9]*$")
IMAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/@-]*$")


class StyledFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, field in self.fields.items():
            if not isinstance(field.widget, forms.RadioSelect | forms.CheckboxInput):
                field.widget.attrs["class"] = "dt-input"
            if field.help_text:
                field.widget.attrs["aria-describedby"] = f"id_{name}_helptext"

    def prepare_errors(self):
        for name, field in self.fields.items():
            if self.is_bound and self[name].errors:
                field.widget.attrs["aria-invalid"] = "true"
                described = field.widget.attrs.get("aria-describedby", "")
                field.widget.attrs["aria-describedby"] = f"{described} id_{name}_error".strip()


class DockerImageForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DockerImage
        fields = [
            "name",
            "friendly_name",
            "description",
            "is_user_visible",
            "setup_commands",
            "base_install_command",
            "install_command_prefix",
            "run_script_template",
        ]
        labels = {"name": "Image reference", "is_user_visible": "Approved for user selection"}
        help_texts = {
            "name": "A Docker image reference, such as python:3.13-alpine. Existing references cannot be renamed.",
            "setup_commands": "Reusable commands run in their configured order before image-specific setup.",
            "base_install_command": "Trusted administrator shell command run after the reusable setup commands.",
            "install_command_prefix": "Trusted package installation command, such as pip install. Validated package names are appended.",
            "run_script_template": "Optional run.sh template. Users must explicitly choose to replace their current file.",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "base_install_command": forms.Textarea(attrs={"rows": 3}),
            "install_command_prefix": forms.TextInput(),
            "run_script_template": forms.Textarea(attrs={"rows": 5}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk:
            self.fields["name"].disabled = True

    def clean_name(self):
        name = self.cleaned_data["name"].strip()
        if not IMAGE_RE.fullmatch(name):
            raise forms.ValidationError(
                "Enter a Docker image reference without spaces or shell characters."
            )
        return name

    def clean(self):
        data = super().clean()
        if data.get("is_user_visible") and not data.get("friendly_name"):
            self.add_error("friendly_name", "Approved images need a name users can recognize.")
        for name in ("base_install_command", "install_command_prefix", "run_script_template"):
            if "\x00" in data.get(name, ""):
                self.add_error(name, "Null characters are not allowed.")
        return data


class DockerImageSetupCommandForm(StyledFormMixin, forms.ModelForm):
    class Meta:
        model = DockerImageSetupCommand
        fields = ["name", "command", "order"]
        help_texts = {
            "command": "Trusted administrator shell command.",
            "order": "Lower numbers run first.",
        }
        widgets = {"command": forms.Textarea(attrs={"rows": 4})}

    def clean_command(self):
        value = self.cleaned_data["command"]
        if "\x00" in value:
            raise forms.ValidationError("Null characters are not allowed.")
        return value


class ImageSelectForm(StyledFormMixin, forms.Form):
    image = forms.ModelChoiceField(
        queryset=DockerImage.objects.none(),
        required=False,
        empty_label="Use the site's Dockerfile",
        label="Base image",
    )
    packages = forms.CharField(
        required=False,
        max_length=4000,
        help_text="Space-separated package names or pinned versions. The approved image's installation command is used.",
    )
    write_run_sh_file = forms.BooleanField(
        required=False,
        label="Replace run.sh with the image template",
        help_text="This overwrites run.sh, including any changes you have made. Leave this unchecked to keep your file.",
    )

    def __init__(self, *args, site, user, **kwargs):
        self.site = site
        kwargs.setdefault(
            "initial", {"image": site.docker_image_id, "packages": site.image_packages}
        )
        super().__init__(*args, **kwargs)
        images = DockerImage.objects.all()
        if not user.is_superuser:
            images = images.filter(is_user_visible=True)
        self.fields["image"].queryset = images.order_by("friendly_name", "name")

    def clean_packages(self):
        names = self.cleaned_data.get("packages", "").split()
        if len(names) > 100 or any(
            len(name) > 100 or not PACKAGE_RE.fullmatch(name) for name in names
        ):
            raise forms.ValidationError(
                "Use up to 100 package names containing letters, numbers, hyphens, underscores, periods, or version operators."
            )
        return " ".join(dict.fromkeys(names))

    def clean(self):
        data = super().clean()
        image = data.get("image")
        if data.get("packages") and (not image or not image.install_command_prefix.strip()):
            self.add_error(
                "packages", "Select an approved image with a package installation command first."
            )
        if data.get("write_run_sh_file") and (not image or not image.run_script_template):
            self.add_error(
                "write_run_sh_file", "The selected image does not provide a run.sh template."
            )
        return data


def memory_bytes(value):
    """Parse positive byte limits using familiar decimal or binary suffixes."""
    match = re.fullmatch(r"\s*(\d+)\s*(B|KB|MB|GB|KiB|MiB|GiB)?\s*", str(value), re.IGNORECASE)
    if not match:
        raise forms.ValidationError("Enter bytes or a size such as 512 MiB or 1 GB.")
    factors = {
        "b": 1,
        "kb": 1000,
        "mb": 1000**2,
        "gb": 1000**3,
        "kib": 1024,
        "mib": 1024**2,
        "gib": 1024**3,
    }
    result = int(match[1]) * factors.get((match[2] or "b").lower(), 1)
    if not 0 < result <= 2**63 - 1:
        raise forms.ValidationError("Enter a positive limit within the supported range.")
    return result


class SiteResourceLimitsForm(StyledFormMixin, forms.ModelForm):
    cpus = forms.FloatField(required=False, min_value=0.001, max_value=3, label="CPU cores")
    memory = forms.CharField(
        required=False,
        max_length=32,
        help_text="For example, 512 MiB. Leave blank to use the default.",
    )
    max_request_body_size = forms.CharField(
        required=False,
        max_length=32,
        label="Maximum request body size",
        help_text="For example, 10 MiB. Leave blank to use the default.",
    )

    class Meta:
        model = SiteResourceLimits
        fields = ["cpus", "memory", "max_request_body_size", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 3})}

    def clean_cpus(self):
        value = self.cleaned_data.get("cpus")
        if value is not None and not math.isfinite(value):
            raise forms.ValidationError("Enter a finite number of CPU cores.")
        return value

    def clean_memory(self):
        value = self.cleaned_data.get("memory", "")
        return str(memory_bytes(value)) if value.strip() else ""

    def clean_max_request_body_size(self):
        value = self.cleaned_data.get("max_request_body_size", "")
        return memory_bytes(value) if value.strip() else None

    @property
    def defaults(self):
        return {
            "cpus": settings.DIRECTOR_RESOURCES_DEFAULT_CPUS,
            "memory": settings.DIRECTOR_RESOURCES_DEFAULT_MEMORY_LIMIT,
            "body": settings.DIRECTOR_RESOURCES_MAX_REQUEST_BODY,
        }
