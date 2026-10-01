from django.db import migrations

from tasks.services import create_system_groups


def forwards(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    TagGroup = apps.get_model("tasks", "TagGroup")
    Tag = apps.get_model("tasks", "Tag")
    Task = apps.get_model("tasks", "Task")
    for ws in Workspace.objects.all():
        statuses, priorities = create_system_groups(ws, TagGroup, Tag)
        for old, tag in zip(("todo", "in_progress", "done"), statuses):
            Task.objects.filter(workspace=ws, status=old).update(status_tag=tag)
        for old, tag in zip((1, 2, 3), priorities):
            Task.objects.filter(workspace=ws, priority=old).update(priority_tag=tag)


class Migration(migrations.Migration):
    dependencies = [("tasks", "0002_system_tag_groups"), ("workspaces", "0001_initial")]

    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
