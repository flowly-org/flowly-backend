"""Getting-started guide: which optional pieces the user has set up (all checks are cheap EXISTS queries)."""

from django.db.models import Q
from rest_framework.response import Response
from rest_framework.views import APIView

#: In display order; the frontend owns texts and links.
STEPS = ["task", "project", "tags", "team", "calendar", "home", "integration", "concierge", "assistant", "time"]


def progress(user) -> dict:
    from calendars.models import CalendarAccount
    from concierge.models import ConciergeProfile
    from integrations.models import Integration
    from mcpauth.models import OAuthGrant
    from tasks.models import Project, Tag, TagGroup, Task
    from timetracking.models import TimeEntry
    from workspaces.models import Invitation, Membership, Workspace

    workspaces = Workspace.objects.filter(memberships__user=user)
    return {
        "task": Task.objects.filter(workspace__in=workspaces).filter(Q(created_by=user) | Q(assignee=user)).exists(),
        "project": Project.objects.filter(workspace__in=workspaces).exists(),
        "tags": Tag.objects.filter(workspace__in=workspaces)
        .filter(Q(group__isnull=True) | Q(group__kind=TagGroup.Kind.CUSTOM))
        .exists(),
        "team": Membership.objects.filter(workspace__in=workspaces, user__is_bot=False).exclude(user=user).exists()
        or Invitation.objects.filter(workspace__in=workspaces).exists(),
        "calendar": CalendarAccount.objects.filter(user=user).exists(),
        "home": bool(user.home_location),
        "integration": Integration.objects.filter(workspace__in=workspaces)
        .exclude(kind__in=["openrouter", "openai_compatible"])
        .exists(),
        "concierge": ConciergeProfile.objects.filter(user=user, telegram_chat_id__isnull=False).exists(),
        "assistant": user.api_tokens.exists() or OAuthGrant.objects.filter(user=user, revoked=False).exists(),
        "time": TimeEntry.objects.filter(user=user).exists(),
    }


class GuideView(APIView):
    """GET → {"steps": [{"key", "done"}], "done": n, "total": n, "pro": bool}"""

    def get(self, request):
        state = progress(request.user)
        steps = [{"key": k, "done": state[k]} for k in STEPS]
        return Response({
            "steps": steps, "done": sum(s["done"] for s in steps), "total": len(steps), "pro": request.user.has_pro,
        })
