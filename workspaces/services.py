from django.db import transaction

from .models import WORKSPACE_COLORS, Invitation, Membership, Workspace


@transaction.atomic
def create_workspace(name, owner):
    # Cycle through the palette so a user's workspaces start out distinguishable.
    count = Membership.objects.filter(user=owner).count()
    workspace = Workspace.objects.create(name=name, created_by=owner, color=WORKSPACE_COLORS[count % len(WORKSPACE_COLORS)])
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
    from tasks.models import TaskShare

    TaskShare.objects.filter(email__iexact=user.email, user__isnull=True).update(user=user)
