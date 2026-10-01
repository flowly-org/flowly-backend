import secrets

from django.db import migrations, models

import workspaces.models


def fill(apps, schema_editor):
    Invitation = apps.get_model("workspaces", "Invitation")
    for inv in Invitation.objects.filter(token__isnull=True):
        inv.token = secrets.token_urlsafe(24)
        inv.save(update_fields=["token"])


class Migration(migrations.Migration):
    dependencies = [("workspaces", "0003_spread_workspace_colors")]

    operations = [
        migrations.AddField(model_name="invitation", name="token", field=models.CharField(max_length=64, null=True, editable=False)),
        migrations.RunPython(fill, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="invitation",
            name="token",
            field=models.CharField(default=workspaces.models.new_invite_token, editable=False, max_length=64, unique=True),
        ),
    ]
