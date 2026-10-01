import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tasks", "0001_initial")]

    operations = [
        migrations.AddField(
            model_name="taggroup",
            name="kind",
            field=models.CharField(
                choices=[("custom", "Custom"), ("status", "Status"), ("priority", "Priority")],
                default="custom",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="tag",
            name="is_done",
            field=models.BooleanField(
                default=False, help_text="Status tags only: tasks in this status count as finished."
            ),
        ),
        migrations.AddConstraint(
            model_name="taggroup",
            constraint=models.UniqueConstraint(
                condition=models.Q(("kind", "custom"), _negated=True),
                fields=("workspace", "kind"),
                name="unique_system_tag_group",
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="status_tag",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.PROTECT, related_name="+", to="tasks.tag"
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="priority_tag",
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="+", to="tasks.tag"
            ),
        ),
    ]
