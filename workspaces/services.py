from django.db import transaction

from .models import Invitation, Membership, Workspace


@transaction.atomic
def create_workspace(name, owner):
    workspace = Workspace.objects.create(name=name, created_by=owner)
    Membership.objects.create(workspace=workspace, user=owner, role=Membership.Role.OWNER)
    from tasks.services import create_system_groups

    create_system_groups(workspace)
    return workspace


@transaction.atomic
def onboard_user(user):
    """Accept pending invitations and give the user a personal workspace."""
    for invitation in Invitation.objects.filter(email__iexact=user.email):
        Membership.objects.get_or_create(
            workspace=invitation.workspace, user=user, defaults={"role": invitation.role}
        )
        invitation.delete()
    create_workspace("Personal", user)
