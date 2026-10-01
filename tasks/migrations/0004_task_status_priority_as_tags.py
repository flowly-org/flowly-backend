import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tasks", "0003_populate_system_tag_groups")]

    operations = [
        migrations.RemoveField(model_name="task", name="status"),
        migrations.RemoveField(model_name="task", name="priority"),
        migrations.RenameField(model_name="task", old_name="status_tag", new_name="status"),
        migrations.RenameField(model_name="task", old_name="priority_tag", new_name="priority"),
        migrations.AlterField(
            model_name="task",
            name="status",
            field=models.ForeignKey(
                help_text="A tag of the STATUS group.",
                on_delete=django.db.models.deletion.PROTECT,
                related_name="status_tasks",
                to="tasks.tag",
            ),
        ),
        migrations.AlterField(
            model_name="task",
            name="priority",
            field=models.ForeignKey(
                blank=True,
                help_text="A tag of the PRIORITY group; empty = no priority.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="priority_tasks",
                to="tasks.tag",
            ),
        ),
    ]
