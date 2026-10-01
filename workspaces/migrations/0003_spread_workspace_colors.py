from django.db import migrations

COLORS = ["#4f46e5", "#0ea5e9", "#14b8a6", "#22c55e", "#eab308", "#f97316", "#ef4444", "#ec4899", "#a855f7", "#64748b"]


def spread(apps, schema_editor):
    Workspace = apps.get_model("workspaces", "Workspace")
    for i, ws in enumerate(Workspace.objects.order_by("id")):
        ws.color = COLORS[i % len(COLORS)]
        ws.save(update_fields=["color"])


class Migration(migrations.Migration):
    dependencies = [("workspaces", "0002_workspace_color")]
    operations = [migrations.RunPython(spread, migrations.RunPython.noop)]
