"""Small studio-only forms for the content area (hosts reuse ``admin.HostForm``)."""
from django import forms

from webinars.models import Topic


class TopicForm(forms.ModelForm):
    class Meta:
        model = Topic
        fields = ("name", "slug")
