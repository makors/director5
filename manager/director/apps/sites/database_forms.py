"""Configured database selection and bounded SQL input."""

from django import forms

from .models import DatabaseHost


class DatabaseHostChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, host):
        return f"{host.get_dbms_display()} · {host.hostname}:{host.port}"


class CreateDatabaseForm(forms.Form):
    host = DatabaseHostChoiceField(
        queryset=DatabaseHost.objects.none(),
        label="Database server",
        empty_label=None,
        widget=forms.Select(attrs={"class": "dt-input"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["host"].queryset = DatabaseHost.objects.order_by("dbms", "hostname", "pk")


class QueryDatabaseForm(forms.Form):
    sql = forms.CharField(
        label="SQL query",
        max_length=100_000,
        widget=forms.Textarea(attrs={"class": "dt-input", "rows": 7, "spellcheck": "false"}),
        help_text="Queries run as this site's database user. Changes are committed when the query succeeds.",
    )
