import django_filters
from django.db.models import Q

from .models import Task


class TaskFilter(django_filters.FilterSet):
    day = django_filters.DateFilter(
        method="filter_day",
        help_text="Tasks planned for this day. Add include_overdue=true to also get "
        "unfinished tasks planned before it.",
    )
    include_overdue = django_filters.BooleanFilter(method="noop")
    date_from = django_filters.DateFilter(field_name="planned_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="planned_date", lookup_expr="lte")
    unscheduled = django_filters.BooleanFilter(field_name="planned_date", lookup_expr="isnull")
    assignee = django_filters.CharFilter(method="filter_assignee", help_text='User id, "me" or "none".')
    project = django_filters.CharFilter(method="filter_project", help_text='Project id or "none".')
    tag = django_filters.NumberFilter(field_name="tags")
    status = django_filters.BaseInFilter(field_name="status", lookup_expr="in", help_text="Status tag ids, comma separated.")
    done = django_filters.BooleanFilter(field_name="status__is_done")
    q = django_filters.CharFilter(method="filter_search")
    workspace = django_filters.NumberFilter(field_name="workspace")
    span_from = django_filters.DateFilter(method="filter_span_from", help_text="Planned or due on/after (timeline).")
    span_to = django_filters.DateFilter(method="filter_span_to", help_text="Planned or due on/before (timeline).")

    class Meta:
        model = Task
        fields = []

    def noop(self, queryset, name, value):
        return queryset

    def filter_day(self, queryset, name, value):
        condition = Q(planned_date=value)
        if self.form.cleaned_data.get("include_overdue"):
            condition |= Q(planned_date__lt=value) & Q(status__is_done=False)
        return queryset.filter(condition)

    def filter_assignee(self, queryset, name, value):
        if value == "me":
            return queryset.filter(assignee=self.request.user)
        if value == "none":
            return queryset.filter(assignee__isnull=True)
        if value.isdigit():
            return queryset.filter(assignee_id=int(value))
        return queryset.none()

    def filter_project(self, queryset, name, value):
        if value == "none":
            return queryset.filter(project__isnull=True)
        if value.isdigit():
            return queryset.filter(project_id=int(value))
        return queryset.none()

    def filter_span_from(self, queryset, name, value):
        return queryset.filter(Q(planned_date__gte=value) | Q(due_date__gte=value))

    def filter_span_to(self, queryset, name, value):
        return queryset.filter(Q(planned_date__lte=value) | Q(due_date__lte=value))

    def filter_search(self, queryset, name, value):
        return queryset.filter(Q(title__icontains=value) | Q(description__icontains=value))
