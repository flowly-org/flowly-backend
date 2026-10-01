from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Count, OuterRef, Subquery
from drf_spectacular.utils import extend_schema
from rest_framework import mixins, permissions, status, viewsets
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import Invitation, Membership, Workspace
from .permissions import WorkspaceScopedMixin
from .serializers import (
    AddMemberSerializer,
    InvitationSerializer,
    MembershipSerializer,
    WorkspaceSerializer,
)
from .services import create_workspace

User = get_user_model()


class WorkspaceViewSet(viewsets.ModelViewSet):
    serializer_class = WorkspaceSerializer

    def get_queryset(self):
        user = self.request.user
        my_role = Membership.objects.filter(workspace=OuterRef("pk"), user=user).values("role")[:1]
        return (
            Workspace.objects.filter(memberships__user=user)
            .annotate(my_role=Subquery(my_role), member_count=Count("memberships", distinct=True))
            .order_by("name", "id")
        )

    def perform_create(self, serializer):
        workspace = create_workspace(serializer.validated_data["name"], self.request.user)
        if serializer.validated_data.get("color"):
            workspace.color = serializer.validated_data["color"]
            workspace.save(update_fields=["color"])
        serializer.instance = workspace

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        instance = self.get_queryset().get(pk=serializer.instance.pk)
        return Response(self.get_serializer(instance).data, status=status.HTTP_201_CREATED)

    def perform_update(self, serializer):
        if serializer.instance.my_role not in (Membership.Role.OWNER, Membership.Role.ADMIN):
            raise PermissionDenied("Only workspace owners and admins can rename a workspace.")
        serializer.save()

    def perform_destroy(self, instance):
        if instance.my_role != Membership.Role.OWNER:
            raise PermissionDenied("Only the workspace owner can delete it.")
        instance.delete()


class MemberViewSet(
    WorkspaceScopedMixin,
    mixins.ListModelMixin,
    mixins.UpdateModelMixin,
    mixins.DestroyModelMixin,
    viewsets.GenericViewSet,
):
    serializer_class = MembershipSerializer

    def get_queryset(self):
        return Membership.objects.filter(workspace=self.workspace).select_related("user")

    @extend_schema(request=AddMemberSerializer, responses={201: MembershipSerializer, 202: InvitationSerializer})
    def create(self, request, *args, **kwargs):
        """Add an existing user by email, or record an invitation if they have no account yet."""
        self.require_manager()
        payload = AddMemberSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        email = payload.validated_data["email"].lower()
        role = payload.validated_data["role"]

        user = User.objects.filter(email__iexact=email).first()
        if user:
            membership, created = Membership.objects.get_or_create(
                workspace=self.workspace, user=user, defaults={"role": role}
            )
            if not created:
                raise ValidationError({"email": "This user is already a member."})
            return Response(MembershipSerializer(membership).data, status=status.HTTP_201_CREATED)

        invitation, _ = Invitation.objects.update_or_create(
            workspace=self.workspace,
            email=email,
            defaults={"role": role, "invited_by": request.user},
        )
        return Response(InvitationSerializer(invitation, context={"can_manage": True}).data, status=status.HTTP_202_ACCEPTED)

    def perform_update(self, serializer):
        self.require_manager()
        if serializer.instance.role == Membership.Role.OWNER:
            raise PermissionDenied("The owner's role can't be changed.")
        serializer.save()

    @transaction.atomic
    def perform_destroy(self, instance):
        leaving_self = instance.user_id == self.request.user.id
        if not leaving_self:
            self.require_manager()
        if instance.role == Membership.Role.OWNER:
            raise PermissionDenied("The owner can't leave or be removed; delete the workspace instead.")
        self.workspace.tasks.filter(assignee=instance.user).update(assignee=None)
        instance.delete()


class InvitationViewSet(
    WorkspaceScopedMixin, mixins.ListModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    serializer_class = InvitationSerializer

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "can_manage": self.membership.can_manage}

    def get_queryset(self):
        return Invitation.objects.filter(workspace=self.workspace)

    def perform_destroy(self, instance):
        self.require_manager()
        instance.delete()


class InvitationLinkView(APIView):
    """GET /api/invitations/<token>/ (public preview) · POST …/accept/ (signed-in user joins)."""

    def get_permissions(self):
        return [permissions.AllowAny()] if self.request.method == "GET" else [permissions.IsAuthenticated()]

    def load(self, token):
        invitation = Invitation.objects.select_related("workspace", "invited_by").filter(token=token).first()
        if invitation is None or invitation.link_expired:
            raise NotFound("This invitation link is no longer valid. Ask for a new one.")
        return invitation

    def get(self, request, token):
        inv = self.load(token)
        member = request.user.is_authenticated and inv.workspace.memberships.filter(user=request.user).exists()
        return Response(
            {
                "workspace": {"id": inv.workspace_id if member else None, "name": inv.workspace.name, "color": inv.workspace.color},
                "invited_by": (inv.invited_by.full_name or inv.invited_by.email) if inv.invited_by else None,
                "email": inv.email,
                "role": inv.role,
                "already_member": member,
            }
        )

    @transaction.atomic
    def post(self, request, token):
        inv = self.load(token)
        Membership.objects.get_or_create(workspace=inv.workspace, user=request.user, defaults={"role": inv.role})
        inv.delete()
        return Response({"workspace": inv.workspace_id})
