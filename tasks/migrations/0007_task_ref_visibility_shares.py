import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import tasks.models


def fill_refs(apps, schema_editor):
    Task = apps.get_model("tasks", "Task")
    seen = set()
    for task in Task.objects.filter(ref__isnull=True).only("id"):
        ref = tasks.models.new_ref()
        while ref in seen:
            ref = tasks.models.new_ref()
        seen.add(ref)
        Task.objects.filter(pk=task.pk).update(ref=ref)


class Migration(migrations.Migration):
    dependencies = [
        ("tasks", "0006_comments_attachments"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(model_name="task", name="ref", field=models.CharField(max_length=16, null=True, editable=False)),
        migrations.RunPython(fill_refs, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="task",
            name="ref",
            field=models.CharField(default=tasks.models.new_ref, editable=False, max_length=16, unique=True),
        ),
        migrations.AddField(
            model_name="task",
            name="visibility",
            field=models.CharField(
                choices=[("private", "Private"), ("team", "Team"), ("shareable", "Shareable"), ("public", "Public")],
                default="team",
                max_length=10,
            ),
        ),
        migrations.CreateModel(
            name="TaskShare",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to=settings.AUTH_USER_MODEL)),
                ("task", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="shares", to="tasks.task")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="task_shares", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["created_at", "id"]},
        ),
        migrations.AddConstraint(
            model_name="taskshare",
            constraint=models.UniqueConstraint(fields=("task", "email"), name="unique_task_share"),
        ),
    ]
