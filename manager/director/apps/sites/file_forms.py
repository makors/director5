"""Validation shared by the file editor and transfer endpoints."""

from django import forms

MAX_EDIT_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def clean_site_path(value: str) -> str:
    if value.startswith("/") or "\\" in value or len(value) > 4096:
        raise forms.ValidationError("Use a path relative to the site directory.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise forms.ValidationError("Paths cannot contain control characters.")
    parts = value.split("/")
    if ".." in parts or len(parts) > 128:
        raise forms.ValidationError("Paths cannot leave the site directory.")
    if any(part.startswith(".director-tmp-") for part in parts):
        raise forms.ValidationError("This filename is reserved.")
    return "/".join(part for part in parts if part not in {"", "."})


class PathForm(forms.Form):
    path = forms.CharField(required=False, max_length=4096, strip=False)

    def clean_path(self):
        return clean_site_path(self.cleaned_data["path"])


class WriteFileForm(PathForm):
    content = forms.CharField(required=False, max_length=MAX_EDIT_BYTES, strip=False)
    expected_sha256 = forms.RegexField(r"^[a-f0-9]{64}$", required=False)
    create_only = forms.BooleanField(required=False)
    mode = forms.RegexField(r"^[0-7]{3}$", required=False)

    def clean_content(self):
        value = self.cleaned_data["content"]
        if len(value.encode("utf-8")) > MAX_EDIT_BYTES:
            raise forms.ValidationError("This file is too large for the editor.")
        return value


class MoveFileForm(PathForm):
    destination = forms.CharField(max_length=4096, strip=False)

    def clean_destination(self):
        return clean_site_path(self.cleaned_data["destination"])


class DeleteFileForm(PathForm):
    recursive = forms.BooleanField(required=False)


class ChmodFileForm(PathForm):
    mode = forms.RegexField(r"^[0-7]{3}$")
