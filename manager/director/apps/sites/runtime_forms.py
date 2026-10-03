from django import forms


class TerminalSessionForm(forms.Form):
    kind = forms.ChoiceField(choices=(("shell", "Shell"), ("database", "Database")))
