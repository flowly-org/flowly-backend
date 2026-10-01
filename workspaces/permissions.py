from django.shortcuts import get_object_or_404
from rest_framework.exceptions import PermissionDenied

from .models import Membership


class WorkspaceScopedMixin:
    """For views nested under /workspaces/<workspace_pk>/.

    Resolves `self.membership` / `self.workspace` for the requesting user and
    answers 404 to non-members so workspace ids don't leak.
    """

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        self.membership = get_object_or_404(
            Membership.objects.select_related("workspace"),
            workspace_id=kwargs["workspace_pk"],
            user=request.user,
        )
        self.workspace = self.membership.workspace

    def require_manager(self):
        if not self.membership.can_manage:
            raise PermissionDenied("Only workspace owners and admins can do this.")

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["workspace"] = getattr(self, "workspace", None)
        context["membership"] = getattr(self, "membership", None)
        return context
